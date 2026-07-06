#!/usr/bin/env python3
"""Accessible 3D viewer server.

Features:
- Receives render state from the viewer and renders with CADComparisonRenderer.
- Sends rendered output to a connected braille display using braille_display.py.
- Optionally reads WitMotion IMU orientation and Slider Trinkey position if hardware is present.
- Supports command logging endpoints used by the legacy cube/slider server.
- Opens accessible-3d-viewer.html in the default browser at startup.
"""

from __future__ import annotations

import base64
import copy
import contextlib
import hashlib
import io
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import uuid

import numpy as np
from flask import Flask, Response, has_request_context, jsonify, request, send_file, stream_with_context
from werkzeug.utils import secure_filename
from flask_cors import CORS
from PIL import Image

from .braille_display import (
    _pixels_to_braille_cells,
    _pixels_to_braille_cells_dotpad,
    _MONARCH_LINES,
    _MONARCH_COLS,
    _DOTPAD_LINES,
    _DOTPAD_COLS,
    _DOTPAD_GRAPHIC_CELLS,
)
from .cad_comparison_lib import CADComparisonRenderer

try:
    from .cross_section_algo import load_voxel_grids as _load_voxel_grids
    from .cross_section_algo import recommend_sections as _recommend_sections
    _CROSS_SECTION_ALGO_AVAILABLE = True
    _CROSS_SECTION_ALGO_ERROR: str | None = None
except Exception as _algo_import_err:
    _load_voxel_grids = None
    _recommend_sections = None
    _CROSS_SECTION_ALGO_AVAILABLE = False
    _CROSS_SECTION_ALGO_ERROR = str(_algo_import_err)
from src.converter.render_low_res import save_binary_array_as_vector_pdf

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except Exception:
    serial = None
    list_ports = None


logging.getLogger("werkzeug").setLevel(logging.ERROR)

app = Flask(__name__)
CORS(app)


@dataclass
class RuntimeState:
    cube_value: str = "z+"
    slider_value: int = 0
    current_model_index: int = 0


if getattr(sys, "frozen", False):
    # In bundled mode, runtime assets are expected next to the executable.
    REPO_ROOT = Path(sys.executable).resolve().parent
else:
    # app/server.py lives one level below the project root.
    REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = REPO_ROOT / "data" / "models"
RENDERS_DIR = REPO_ROOT / "data" / "renders"
STUDY_LOG_DIR = REPO_ROOT / "data" / "logs"
BRAILLE_LOG_PATH = Path(os.getenv("BRAILLE_LOG_PATH", str(STUDY_LOG_DIR / "braille_send_events.jsonl")))


def _is_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False

    # Write/unlink probe catches bind mounts that exist but are not writable.
    probe = path / f".cad_a11y_write_test_{uuid.uuid4().hex}"
    try:
        with probe.open("wb") as handle:
            handle.write(b"ok")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        with contextlib.suppress(Exception):
            probe.unlink(missing_ok=True)
        return False


def _resolve_upload_dir() -> Path:
    env_dir = os.getenv("UPLOAD_MODEL_DIR", "").strip()
    candidates: list[Path] = []
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(
        [
            MODEL_DIR,
            REPO_ROOT / "data" / "uploads",
            Path("/tmp/cad-a11y/models"),
        ]
    )

    # Preserve order while removing duplicates.
    deduped_candidates = list(dict.fromkeys(candidates))
    for candidate in deduped_candidates:
        if _is_writable_directory(candidate):
            return candidate
    return MODEL_DIR


UPLOAD_DIR = _resolve_upload_dir()

DEFAULT_RENDER_PARAMS: dict[str, Any] = {
    "view": "y-",
    "zoom": "0",
    "depth": 0,
    "renderMode": "Outline",
    "mode": "single",
    "move_camera_center": "none",
    "print_view": False,
}

# WitMotion IMU orientation → view mapping
_FACE_NORMALS = np.array([
    [1, 0, 0],   # x+
    [-1, 0, 0],  # x-
    [0, 1, 0],   # y+
    [0, -1, 0],  # y-
    [0, 0, 1],   # z+
    [0, 0, -1],  # z-
], dtype=float)
_FACE_NAMES = ["x+", "x-", "y+", "y-", "z+", "z-"]
_WORLD_UP = np.array([0.0, 0.0, 1.0])


def _euler_to_rotation_matrix(roll_deg: float, pitch_deg: float, yaw_deg: float) -> np.ndarray:
    r, p, y = np.radians(roll_deg), np.radians(pitch_deg), np.radians(yaw_deg)
    Rx = np.array([[1, 0, 0], [0, np.cos(r), -np.sin(r)], [0, np.sin(r), np.cos(r)]])
    Ry = np.array([[np.cos(p), 0, np.sin(p)], [0, 1, 0], [-np.sin(p), 0, np.cos(p)]])
    Rz = np.array([[np.cos(y), -np.sin(y), 0], [np.sin(y), np.cos(y), 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def _orientation_to_view(roll_deg: float, pitch_deg: float, yaw_deg: float) -> str:
    R = _euler_to_rotation_matrix(roll_deg, pitch_deg, yaw_deg)
    dots = (_FACE_NORMALS @ R.T) @ _WORLD_UP
    return _FACE_NAMES[int(np.argmax(dots))]

state = RuntimeState()
renderers_by_model: dict[int, CADComparisonRenderer] = {}
current_render: np.ndarray | None = None
commands_log: list[dict[str, Any]] = []
state_lock = threading.Lock()
models_lock = threading.Lock()
# Serialize all engine.render() calls — matplotlib is not thread-safe, and
# engine.render() mutates view_current_camera_center (camera pan state).
# Concurrent renders corrupt that state, producing blank or wrong-view output.
render_lock = threading.Lock()
braille_log_lock = threading.Lock()
commands_log_lock = threading.Lock()
braille_send_sequence = 0
last_render_fingerprint: str | None = None
last_render_response: dict[str, Any] | None = None
RENDER_QUANTIZED_CACHE_MAX = int(os.getenv("RENDER_QUANTIZED_CACHE_MAX", "128"))
quantized_render_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
quantized_render_cache_lock = threading.Lock()
PREVIEW_PAYLOAD_CACHE_MAX = int(os.getenv("PREVIEW_PAYLOAD_CACHE_MAX", "128"))
preview_payload_cache: OrderedDict[str, np.ndarray] = OrderedDict()
preview_payload_cache_lock = threading.Lock()

# SSE client registry — each connected browser tab gets its own queue.
import queue as _queue_module
_sse_clients: list[_queue_module.Queue] = []
_sse_clients_lock = threading.Lock()

# Quiet-by-default: set SERVER_VERBOSE=1 to see detailed logs.
QUIET_MODE = os.getenv("SERVER_VERBOSE", "0").lower() not in {"1", "true", "yes", "on"}


def _log(message: str, *, force: bool = False) -> None:
    if force or not QUIET_MODE:
        print(message)


def _push_sse(data: dict) -> None:
    """Broadcast a hardware-state event to all connected SSE clients."""
    message = f"data: {json.dumps(data)}\n\n"
    with _sse_clients_lock:
        stale = []
        for q in _sse_clients:
            try:
                q.put_nowait(message)
            except _queue_module.Full:
                stale.append(q)
        for q in stale:
            _sse_clients.remove(q)


def _renderer_stdio_guard():
    if QUIET_MODE:
        sink = io.StringIO()
        return contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink)
    return contextlib.nullcontext(), contextlib.nullcontext()


def _discover_models() -> list[Path]:
    patterns = ("*.stl", "*.step", "*.STEP")
    models: list[Path] = []
    search_dirs = list(dict.fromkeys([MODEL_DIR, UPLOAD_DIR]))
    for model_dir in search_dirs:
        for pattern in patterns:
            models.extend(sorted(model_dir.glob(pattern)))
    # Deduplicate while preserving order.
    return list(dict.fromkeys(models))


def _find_default_model() -> Path:
    models = _discover_models()
    if models:
        return models[0]
    raise FileNotFoundError(f"No .stl/.step model found in {MODEL_DIR}")


DEFAULT_MODEL = _find_default_model()
AVAILABLE_MODELS = _discover_models() or [DEFAULT_MODEL]
MODEL_NAME_LIST = [model_path.stem for model_path in AVAILABLE_MODELS]
_model_list_last_refresh: float = 0.0
_MODEL_LIST_REFRESH_INTERVAL = 2.0  # seconds


def _refresh_model_list_if_stale() -> None:
    """Refresh AVAILABLE_MODELS/MODEL_NAME_LIST from disk at most every 2 s."""
    global AVAILABLE_MODELS, MODEL_NAME_LIST, _model_list_last_refresh
    now = time.monotonic()
    if now - _model_list_last_refresh < _MODEL_LIST_REFRESH_INTERVAL:
        return
    with models_lock:
        if now - _model_list_last_refresh < _MODEL_LIST_REFRESH_INTERVAL:
            return  # another thread already refreshed
        AVAILABLE_MODELS = _discover_models() or [DEFAULT_MODEL]
        MODEL_NAME_LIST = [p.stem for p in AVAILABLE_MODELS]
        _model_list_last_refresh = time.monotonic()


def _normalize_model_index(raw_index: Any) -> int:
    if raw_index is None:
        with state_lock:
            return state.current_model_index
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        return 0
    if index < 0 or index >= len(AVAILABLE_MODELS):
        return 0
    return index


def get_or_create_renderer(model_index: int | None = None) -> CADComparisonRenderer:
    index = _normalize_model_index(model_index)
    with models_lock:
        if index not in renderers_by_model:
            model_path = AVAILABLE_MODELS[index]
            _log(f"Initializing CAD renderer with: {model_path}")
            out_guard, err_guard = _renderer_stdio_guard()
            with out_guard, err_guard:
                renderer = CADComparisonRenderer(
                    str(model_path),
                    str(model_path),
                    #defer_slice_graph_precompute=True,
                )
                renderer.start_background_slice_precompute()
                renderers_by_model[index] = renderer
        return renderers_by_model[index]


def _to_braille_payload(rendered_rgba: np.ndarray) -> np.ndarray:
    # Convert renderer output to braille payload using a single deterministic
    # rule for all modes: any non-white pixel is raised.
    channel = rendered_rgba[:, :, 0].astype(np.uint8, copy=False)
    return np.where(channel < 255, 255, 0).astype(np.uint8)


def _payload_stats(payload: np.ndarray) -> dict[str, Any]:
    total_cells = int(payload.size)
    raised_cells = int(np.count_nonzero(payload > 0))
    payload_bytes = payload.astype(np.uint8, copy=False).tobytes()
    return {
        "shape": list(payload.shape),
        "dtype": str(payload.dtype),
        "total_cells": total_cells,
        "raised_cells": raised_cells,
        "raised_ratio": float(raised_cells / total_cells) if total_cells else 0.0,
        "sum": int(np.sum(payload, dtype=np.int64)),
        "min": int(np.min(payload)) if total_cells else 0,
        "max": int(np.max(payload)) if total_cells else 0,
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }


def _collect_request_context() -> dict[str, Any]:
    if not has_request_context():
        return {}
    return {
        "endpoint": request.path,
        "method": request.method,
        "remote_addr": request.remote_addr,
        "user_agent": request.user_agent.string,
    }


def _write_braille_event(event: dict[str, Any]) -> None:
    BRAILLE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True, ensure_ascii=False)
    with braille_log_lock:
        with BRAILLE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _next_braille_send_sequence() -> int:
    global braille_send_sequence
    with braille_log_lock:
        braille_send_sequence += 1
        return braille_send_sequence


def _make_hifi_preview(
    params: dict[str, Any], model_index: int, preview_width: int = 800, *, use_cache: bool = True
) -> tuple[str, list[int]]:
    """Render at high resolution and return (base64_png, [height, width]).

    Return strict binary black-on-white preview (no grayscale).
    """
    engine = get_or_create_renderer(model_index)
    orig = list(engine.screen_size) if engine.screen_size else [96, 40]
    w0, h0 = max(1, orig[0]), max(1, orig[1] if len(orig) > 1 else orig[0])
    hifi_h = max(1, int(round(preview_width * h0 / w0)))

    payload = _get_braille_payload_at_size(
        params,
        model_index=model_index,
        pixel_width=preview_width,
        pixel_height=hifi_h,
        use_cache=use_cache,
    )
    # Preview is visual black-on-white while payload semantics are raised=255.
    preview_bw = np.where(payload > 0, 0, 255).astype(np.uint8)
    return _img_to_base64_png(preview_bw), list(preview_bw.shape)


def _render_and_send(
    params: dict[str, Any], *, source: str, model_index: int
) -> tuple[np.ndarray, list[float] | None, np.ndarray]:
    global current_render

    engine = get_or_create_renderer(model_index)
    out_guard, err_guard = _renderer_stdio_guard()
    with render_lock:
        with out_guard, err_guard:
            rendered = engine.render(params)
    current_render = rendered

    braille_payload = _to_braille_payload(rendered)
    sequence = _next_braille_send_sequence()
    with state_lock:
        state_snapshot = {
            "cube_value": state.cube_value,
            "slider_value": state.slider_value,
            "current_model_index": state.current_model_index,
        }
    event: dict[str, Any] = {
        "event": "braille_send",
        "sequence": sequence,
        "source": source,
        "input_source": params.get("input_source", "unknown"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "model": str(AVAILABLE_MODELS[model_index]),
        "model_index": model_index,
        "params": {
            "view": params.get("view"),
            "zoom": params.get("zoom"),
            "depth": params.get("depth"),
            "renderMode": params.get("renderMode"),
            "mode": params.get("mode"),
            "move_camera_center": params.get("move_camera_center"),
            "print_view": params.get("print_view"),
        },
        "state": state_snapshot,
        "render_shape": list(rendered.shape),
        "payload": _payload_stats(braille_payload),
        "request": _collect_request_context(),
    }

    # Device sends are handled browser-side (Web HID / Web BLE).
    event.update({"status": "success", "send_duration_ms": 0.0})
    _write_braille_event(event)

    bbox = getattr(engine, "bbox", None)
    return rendered, bbox, braille_payload


def _save_print_if_requested(params: dict[str, Any], engine: CADComparisonRenderer, img_data: np.ndarray) -> None:
    if not params.get("print_view"):
        return

    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    next_index = 0
    for file_path in RENDERS_DIR.glob("print_*.pdf"):
        parts = file_path.stem.split("_")
        if len(parts) < 2:
            continue
        try:
            idx = int(parts[1])
            next_index = max(next_index, idx + 1)
        except ValueError:
            continue

    stem = (
        f"print_{next_index}_"
        f"{engine.current_render_mode}_"
        f"{engine.current_cut_depth}_"
        f"{engine.view_current_axis}_"
        f"{np.array(engine.view_current_view_limits).tolist()}"
    )
    pdf_path = RENDERS_DIR / f"{stem}.pdf"
    npy_path = RENDERS_DIR / f"{stem}.npy"
    save_binary_array_as_vector_pdf(img_data, str(pdf_path))
    with npy_path.open("wb") as handle:
        np.save(handle, img_data)


def _img_to_base64_png(img_array: np.ndarray) -> str:
    image = Image.fromarray(img_array.astype("uint8"))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def _img_to_png_bytes(img_array: np.ndarray) -> io.BytesIO:
    """Return an in-memory PNG file (BytesIO) for send_file."""
    image = Image.fromarray(img_array.astype("uint8"))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def _prepare_render_params(data: dict[str, Any] | None) -> tuple[dict[str, Any], int, bool, str]:
    """Merge incoming request data with defaults and return (params, model_index, is_pan, fingerprint)."""
    if data is None:
        data = {}
    merged = dict(DEFAULT_RENDER_PARAMS)
    merged.update({k: v for k, v in data.items() if v is not None})

    # Normalise view to lowercase string.
    merged["view"] = str(merged.get("view", "")).lower()

    orientation = merged.get("orientation")
    if isinstance(orientation, dict):
        def _vec3(value: Any) -> list[float] | None:
            if not isinstance(value, (list, tuple)) or len(value) != 3:
                return None
            out: list[float] = []
            for component in value:
                try:
                    out.append(float(component))
                except (TypeError, ValueError):
                    return None
            return out

        forward = _vec3(orientation.get("forward"))
        up = _vec3(orientation.get("up"))
        right = _vec3(orientation.get("right"))
        if forward is not None and up is not None and right is not None:
            merged["orientation"] = {
                "scheme": str(orientation.get("scheme", "basis-v1")),
                "forward": forward,
                "up": up,
                "right": right,
            }
        else:
            merged["orientation"] = None
    else:
        merged["orientation"] = None

    camera_center = merged.get("camera_center")
    if isinstance(camera_center, (list, tuple)) and len(camera_center) == 2:
        try:
            merged["camera_center"] = [float(camera_center[0]), float(camera_center[1])]
        except (TypeError, ValueError):
            merged["camera_center"] = None
    else:
        merged["camera_center"] = None

    world_camera_center = merged.get("world_camera_center")
    if isinstance(world_camera_center, (list, tuple)) and len(world_camera_center) == 3:
        try:
            merged["world_camera_center"] = [
                float(world_camera_center[0]),
                float(world_camera_center[1]),
                float(world_camera_center[2]),
            ]
        except (TypeError, ValueError):
            merged["world_camera_center"] = None
    else:
        merged["world_camera_center"] = None

    model_index = _normalize_model_index(data.get("current_model"))

    # Camera moves are computed relative to the supplied camera_center, so they
    # must bypass cache lookup; otherwise a move request can hit a cached image
    # for the pre-move center and appear to do nothing.
    is_pan_request = str(merged.get("move_camera_center", "none")).lower() != "none"

    # Fingerprint for caching (excludes transient fields).
    fp_keys = ("view", "zoom", "depth", "renderMode", "projectionMode", "mode", "current_model",
               "orientation",
               "camera_center",
               "world_camera_center",
               "compose_scrollbar", "compose_slicegraph", "show_view_info_box",
               "output_device", "slicegraph_locked", "slicegraph_view", "slicegraph_depth", "slicegraph_mode")
    fp_dict = {k: merged.get(k) for k in fp_keys}
    fp_dict["model_index"] = model_index
    fingerprint = hashlib.sha256(json.dumps(fp_dict, sort_keys=True).encode()).hexdigest()

    return merged, model_index, is_pan_request, fingerprint


def _build_quantized_render_key(params: dict[str, Any], model_index: int) -> str:
    """Build a stable coarse key for near-identical interactive requests."""
    quantized = {
        "model_index": model_index,
        "view": str(params.get("view", "")).lower(),
        "orientation": params.get("orientation"),
        "camera_center": params.get("camera_center"),
        "world_camera_center": params.get("world_camera_center"),
        "depth": round(float(params.get("depth", 0)), 0),
        "zoom": round(float(params.get("zoom", 0.0)), 2),
        "renderMode": str(params.get("renderMode", "")).lower(),
        "projectionMode": str(params.get("projectionMode", "orthographic")).lower(),
        "mode": str(params.get("mode", "single")).lower(),
        "compose_scrollbar": bool(params.get("compose_scrollbar", False)),
        "compose_slicegraph": bool(params.get("compose_slicegraph", False)),
        "slicegraph_locked": bool(params.get("slicegraph_locked", False)),
        "slicegraph_view": str(params.get("slicegraph_view", "")).lower(),
        "slicegraph_depth": round(float(params.get("slicegraph_depth", 0)), 0),
        "slicegraph_mode": str(params.get("slicegraph_mode", "difference")).lower(),
    }
    return hashlib.sha256(json.dumps(quantized, sort_keys=True).encode()).hexdigest()


def _get_quantized_cached_response(cache_key: str) -> dict[str, Any] | None:
    with quantized_render_cache_lock:
        cached = quantized_render_cache.get(cache_key)
        if cached is None:
            return None
        quantized_render_cache.move_to_end(cache_key)
        return copy.deepcopy(cached)


def _set_quantized_cached_response(cache_key: str, response: dict[str, Any]) -> None:
    with quantized_render_cache_lock:
        quantized_render_cache[cache_key] = copy.deepcopy(response)
        quantized_render_cache.move_to_end(cache_key)
        while len(quantized_render_cache) > max(1, RENDER_QUANTIZED_CACHE_MAX):
            quantized_render_cache.popitem(last=False)


def _build_preview_payload_cache_key(
    params: dict[str, Any], model_index: int, pixel_width: int, pixel_height: int
) -> str:
    fp_keys = (
        "view",
        "zoom",
        "depth",
        "renderMode",
        "projectionMode",
        "mode",
        "orientation",
        "camera_center",
        "world_camera_center",
        "compose_scrollbar",
        "compose_slicegraph",
        "show_view_info_box",
        "slicegraph_locked",
        "slicegraph_view",
        "slicegraph_depth",
        "slicegraph_mode",
    )
    fp_dict = {k: params.get(k) for k in fp_keys}
    fp_dict["model_index"] = model_index
    fp_dict["pixel_width"] = int(pixel_width)
    fp_dict["pixel_height"] = int(pixel_height)
    return hashlib.sha256(json.dumps(fp_dict, sort_keys=True).encode()).hexdigest()


def _get_preview_payload_cached(cache_key: str) -> np.ndarray | None:
    with preview_payload_cache_lock:
        cached = preview_payload_cache.get(cache_key)
        if cached is None:
            return None
        preview_payload_cache.move_to_end(cache_key)
        return cached.copy()


def _set_preview_payload_cached(cache_key: str, payload: np.ndarray) -> None:
    with preview_payload_cache_lock:
        preview_payload_cache[cache_key] = payload.copy()
        preview_payload_cache.move_to_end(cache_key)
        while len(preview_payload_cache) > max(1, PREVIEW_PAYLOAD_CACHE_MAX):
            preview_payload_cache.popitem(last=False)


def _render_braille_payload_at_size(
    params: dict[str, Any], *, model_index: int, pixel_width: int, pixel_height: int
) -> np.ndarray:
    engine = get_or_create_renderer(model_index)
    out_guard, err_guard = _renderer_stdio_guard()
    with render_lock:
        original_screen_size = list(engine.screen_size) if engine.screen_size else [96, 40]
        engine.screen_size = [max(1, int(pixel_width)), max(1, int(pixel_height))]
        try:
            with out_guard, err_guard:
                rendered = engine.render(params)
        finally:
            engine.screen_size = original_screen_size
    return _to_braille_payload(rendered)


def _get_braille_payload_at_size(
    params: dict[str, Any], *, model_index: int, pixel_width: int, pixel_height: int, use_cache: bool = True
) -> np.ndarray:
    if not use_cache:
        return _render_braille_payload_at_size(
            params,
            model_index=model_index,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
        )

    cache_key = _build_preview_payload_cache_key(
        params,
        model_index=model_index,
        pixel_width=pixel_width,
        pixel_height=pixel_height,
    )
    cached = _get_preview_payload_cached(cache_key)
    if cached is not None:
        return cached

    payload = _render_braille_payload_at_size(
        params,
        model_index=model_index,
        pixel_width=pixel_width,
        pixel_height=pixel_height,
    )
    _set_preview_payload_cached(cache_key, payload)
    return payload


def _render_response(params: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Render, send to braille display, and build JSON response dict."""
    _refresh_model_list_if_stale()
    model_index = _normalize_model_index(params.get("current_model"))
    rendered, bbox, braille_payload = _render_and_send(params, source=source, model_index=model_index)

    engine = get_or_create_renderer(model_index)
    _save_print_if_requested(params, engine, rendered)

    response: dict[str, Any] = {
        "status": "success",
        "image_base64": _img_to_base64_png(braille_payload),
        "image_shape": list(braille_payload.shape),
        "model_list": MODEL_NAME_LIST,
    }
    debug_info = getattr(engine, "last_render_debug", None)
    if isinstance(debug_info, dict) and debug_info:
        response["debug"] = debug_info
    if bbox is not None:
        response["bbox"] = bbox
    if str(params.get("output_device", "")).strip().lower() == "monarch_hid":
        cells = _pixels_to_braille_cells(braille_payload, lines=_MONARCH_LINES, cols=_MONARCH_COLS)
        response["monarch_cells_hex"] = cells.hex()
    return response


def initialize_default_braille_render() -> None:
    _log("Preparing initial render...", force=True)

    try:
        merged_params, model_index, _is_pan_request, _fingerprint = _prepare_render_params(dict(DEFAULT_RENDER_PARAMS))
        rendered, _, _ = _render_and_send(merged_params, source="startup", model_index=model_index)
        _log(f"Initial render ready: shape={tuple(rendered.shape)}", force=True)
    except Exception as error:
        _log(f"Initial render failed: {error}", force=True)


def _record_command(data: dict[str, Any]) -> int:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "request": _collect_request_context(),
        "data": data,
    }
    with commands_log_lock:
        commands_log.append(entry)
        return len(commands_log)


def open_viewer_in_browser() -> None:
    try:
        webbrowser.open("http://localhost:6969/viewer", new=1)
        _log("Opened viewer: http://localhost:6969/viewer", force=True)
    except Exception as error:
        _log(f"Could not open viewer in browser: {error}", force=True)


def _slider_worker() -> None:
    if serial is None or list_ports is None:
        _log("Serial dependencies unavailable; slider input disabled.", force=True)
        return

    while True:
        trinkey_port = None
        for port in list_ports.comports(include_links=False):
            if getattr(port, "pid", None) == 0x8102:
                trinkey_port = port
                break

        if trinkey_port is None:
            _log("Slider Trinkey not found; slider input disabled.", force=True)
            return

        _log(f"Slider Trinkey found at {trinkey_port.device}", force=True)
        try:
            with serial.Serial(trinkey_port.device, timeout=1) as trinkey:
                # Average 10 consecutive readings to smooth jitter before reporting.
                samples: list[int] = []
                while True:
                    line = trinkey.readline().decode("utf-8", errors="ignore").strip()
                    if not line.startswith("Slider: "):
                        continue
                    try:
                        samples.append(int(float(line.split(": ", maxsplit=1)[1])))
                    except ValueError:
                        continue
                    if len(samples) < 10:
                        continue
                    value = int(sum(samples) / len(samples))
                    samples.clear()
                    with state_lock:
                        state.slider_value = value
                    _push_sse({"slider_value": value})
        except Exception as error:
            _log(f"Slider disconnected ({error}); retrying in 3s...", force=True)
            time.sleep(3)


def _witmotion_worker() -> None:
    """Read WitMotion IMU euler angles via serial and push view updates.

    Supports WT901 and similar devices in ASCII CSV output mode.
    Expected line format: Time,ax,ay,az,wx,wy,wz,Roll(deg),Pitch(deg),Yaw(deg)[,...]
    """
    if serial is None or list_ports is None:
        _log("Serial dependencies unavailable; WitMotion input disabled.", force=True)
        return

    # WitMotion WT901 uses CH340 (vid=0x1a86) or CP2102 (vid=0x10c4) USB-serial chips.
    witmotion_port = None
    for port in list_ports.comports(include_links=False):
        if getattr(port, "vid", None) in {0x1A86, 0x10C4}:
            witmotion_port = port
            break

    if witmotion_port is None:
        _log("WitMotion sensor not found; IMU orientation input disabled.", force=True)
        return

    _log(f"WitMotion sensor found at {witmotion_port.device}", force=True)
    last_view: str | None = None
    try:
        with serial.Serial(witmotion_port.device, baudrate=9600, timeout=1) as sensor:
            while True:
                line = sensor.readline().decode("utf-8", errors="ignore").strip()
                parts = line.split(",")
                if len(parts) < 10:
                    continue
                try:
                    roll = float(parts[7])
                    pitch = float(parts[8])
                    yaw = float(parts[9])
                except (ValueError, IndexError):
                    continue
                view = _orientation_to_view(roll, pitch, yaw)
                if view == last_view:
                    continue
                last_view = view
                with state_lock:
                    state.cube_value = view
                _push_sse({"cube_value": view})
                _log(f"WitMotion orientation ({roll:.1f}, {pitch:.1f}, {yaw:.1f}) -> view {view}")
    except Exception as error:
        _log(f"WitMotion integration disabled after error: {error}", force=True)


def start_optional_hardware_watchers() -> None:
    threading.Thread(target=_slider_worker, daemon=True).start()
    threading.Thread(target=_witmotion_worker, daemon=True).start()


@app.route("/viewer", methods=["GET"])
def serve_viewer():
    """Serve the main HTML viewer (needed for ES module imports)."""
    return send_file(REPO_ROOT / "accessible-3d-viewer.html")


@app.route("/", methods=["GET"])
def home():
    return jsonify(
        {
            "status": "running",
            "message": "Accessible 3D Viewer server",
            "endpoints": {
                "/render": "POST - Render CAD view with parameters",
                "/render/image": "GET - Get last rendered image as PNG",
                "/render/base64": "GET - Get last rendered image as base64",
                "/command": "POST - Receive and log command; auto-render if render params exist",
                "/commands": "GET - Retrieve logged commands",
                "/commands/clear": "POST - Clear command log",
                "/commands/stats": "GET - Command statistics",
                "/models": "GET/POST - List or update active model index",
                "/upload": "POST - Upload an STL or STEP model file",
                "/recommend_sections": "POST - Recommend cross-section planes for the current model",
                "/get_data": "GET - Optional cube/slider state",
                "/render/dotpad-hex": "POST - Get render as DotPad hex string for Web SDK",
                "/viewer": "GET - Serve the HTML viewer (required for DotPad Web SDK)",
            },
        }
    )


@app.route("/render", methods=["POST"])
def render_view():
    global last_render_fingerprint, last_render_response

    try:
        t0 = time.perf_counter()
        _refresh_model_list_if_stale()
        merged_params, model_index, is_pan_request, fingerprint = _prepare_render_params(request.get_json(silent=True))
        quantized_cache_key = _build_quantized_render_key(merged_params, model_index)

        with state_lock:
            if (
                not is_pan_request
                and merged_params.get("print_view") is not True
                and last_render_fingerprint == fingerprint
                and last_render_response is not None
            ):
                last_render_response["model_list"] = MODEL_NAME_LIST
                response = copy.deepcopy(last_render_response)
                debug = dict(response.get("debug", {}))
                debug.update(
                    {
                        "phase1_exact_cache_hit": True,
                        "phase1_quantized_cache_hit": False,
                        "phase1_total_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                    }
                )
                response["debug"] = debug
                return jsonify(response), 200

        if not is_pan_request and merged_params.get("print_view") is not True:
            cached_response = _get_quantized_cached_response(quantized_cache_key)
            if cached_response is not None:
                cached_response["model_list"] = MODEL_NAME_LIST
                debug = dict(cached_response.get("debug", {}))
                debug.update(
                    {
                        "phase1_exact_cache_hit": False,
                        "phase1_quantized_cache_hit": True,
                        "phase1_total_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                    }
                )
                cached_response["debug"] = debug
                return jsonify(cached_response), 200

        # Do not copy browser-selected viewpoint into global hardware state.
        # Global cube_value is reserved for hardware-originated updates (WitMotion IMU)
        # so one browser's manual navigation does not move other connected clients.
        with state_lock:
            state.current_model_index = model_index

        response = _render_response(merged_params, source="http_render")
        debug = dict(response.get("debug", {}))
        debug.update(
            {
                "phase1_exact_cache_hit": False,
                "phase1_quantized_cache_hit": False,
                "phase1_total_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            }
        )
        response["debug"] = debug

        with state_lock:
            if merged_params.get("print_view") is not True:
                last_render_fingerprint = fingerprint
                last_render_response = response
                _set_quantized_cached_response(quantized_cache_key, response)
        return jsonify(response), 200
    except Exception as error:
        _log(f"Error rendering: {error}", force=True)
        return jsonify({"status": "error", "message": str(error)}), 400


@app.route("/render/image", methods=["GET"])
def get_render_image():
    if current_render is None:
        return jsonify({"status": "error", "message": "No image has been rendered yet"}), 404
    try:
        return send_file(_img_to_png_bytes(current_render), mimetype="image/png")
    except Exception as error:
        return jsonify({"status": "error", "message": str(error)}), 400


@app.route("/render/base64", methods=["GET"])
def get_render_base64():
    if current_render is None:
        return jsonify({"status": "error", "message": "No image has been rendered yet"}), 404
    try:
        return jsonify(
            {
                "status": "success",
                "image_base64": _img_to_base64_png(current_render),
                "image_shape": list(current_render.shape),
            }
        ), 200
    except Exception as error:
        return jsonify({"status": "error", "message": str(error)}), 400


@app.route("/command", methods=["POST"])
def receive_command():
    global last_render_fingerprint, last_render_response
    try:
        data = request.get_json(silent=True) or {}
        command_id = _record_command(data)

        response_data: dict[str, Any] = {
            "status": "success",
            "message": "Command received",
            "command_id": command_id,
        }

        render_keys = {"view", "renderMode", "depth", "zoom", "mode", "move_camera_center", "print_view", "current_model"}
        if any(key in data for key in render_keys):
            merged_params, model_index, is_pan_request, fingerprint = _prepare_render_params(data)
            with state_lock:
                state.current_model_index = model_index

            render_result: dict[str, Any] | None = None
            if not is_pan_request and merged_params.get("print_view") is not True:
                with state_lock:
                    if last_render_fingerprint == fingerprint and last_render_response is not None:
                        render_result = copy.deepcopy(last_render_response)
                        render_result["model_list"] = MODEL_NAME_LIST
                if render_result is None:
                    quantized_cache_key = _build_quantized_render_key(merged_params, model_index)
                    render_result = _get_quantized_cached_response(quantized_cache_key)
                    if render_result is not None:
                        render_result["model_list"] = MODEL_NAME_LIST

            if render_result is None:
                render_result = _render_response(merged_params, source="command_auto_render")
                if not is_pan_request and merged_params.get("print_view") is not True:
                    quantized_cache_key = _build_quantized_render_key(merged_params, model_index)
                    with state_lock:
                        last_render_fingerprint = fingerprint
                        last_render_response = render_result
                    _set_quantized_cached_response(quantized_cache_key, render_result)
            response_data["render"] = render_result

        return jsonify(response_data), 200
    except Exception as error:
        _log(f"Error processing command: {error}", force=True)
        return jsonify({"status": "error", "message": str(error)}), 400


@app.route("/commands", methods=["GET"])
def get_commands():
    with commands_log_lock:
        payload = {
            "status": "success",
            "total_commands": len(commands_log),
            "commands": list(commands_log),
        }
    return jsonify(payload), 200


@app.route("/commands/clear", methods=["POST"])
def clear_commands():
    with commands_log_lock:
        count = len(commands_log)
        commands_log.clear()
    return jsonify({"status": "success", "message": f"Cleared {count} commands"}), 200


@app.route("/commands/stats", methods=["GET"])
def get_stats():
    with commands_log_lock:
        snapshot = list(commands_log)

    if not snapshot:
        return jsonify({"status": "success", "total_commands": 0, "stats": {}}), 200

    type_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    for entry in snapshot:
        data = entry.get("data", {})
        if not isinstance(data, dict):
            continue
        cmd_type = str(data.get("type", "unknown"))
        action = str(data.get("action", "unknown"))
        type_counts[cmd_type] = type_counts.get(cmd_type, 0) + 1
        action_counts[action] = action_counts.get(action, 0) + 1

    return jsonify(
        {
            "status": "success",
            "total_commands": len(snapshot),
            "stats": {
                "by_type": type_counts,
                "by_action": action_counts,
                "first_command": snapshot[0].get("timestamp"),
                "last_command": snapshot[-1].get("timestamp"),
            },
        }
    ), 200


@app.route("/models", methods=["GET", "POST"])
def models_endpoint():
    global AVAILABLE_MODELS, MODEL_NAME_LIST, last_render_fingerprint, last_render_response

    if request.method == "GET":
        with models_lock:
            AVAILABLE_MODELS = _discover_models() or [DEFAULT_MODEL]
            MODEL_NAME_LIST = [p.stem for p in AVAILABLE_MODELS]
        with state_lock:
            current_index = state.current_model_index
        return jsonify(
            {
                "status": "success",
                "model_list": MODEL_NAME_LIST,
                "model_paths": [str(model) for model in AVAILABLE_MODELS],
                "current_model": current_index,
            }
        ), 200

    try:
        data = request.get_json(silent=True) or {}
        model_index = _normalize_model_index(data.get("current_model", data.get("model_index")))
        with state_lock:
            state.current_model_index = model_index
            # Changing model invalidates response cache.
            last_render_fingerprint = None
            last_render_response = None
        with quantized_render_cache_lock:
            quantized_render_cache.clear()
        with preview_payload_cache_lock:
            preview_payload_cache.clear()
        return jsonify(
            {
                "status": "success",
                "message": "Current model updated",
                "current_model": model_index,
                "model_name": MODEL_NAME_LIST[model_index],
                "model_path": str(AVAILABLE_MODELS[model_index]),
            }
        ), 200
    except Exception as error:
        return jsonify({"status": "error", "message": str(error)}), 400


_ALLOWED_EXTENSIONS = {".stl", ".step"}
_MAX_UPLOAD_SESSION_ID_LEN = 128

# Tracks uploaded model paths by browser-tab session id so they can be cleaned up
# when the page closes. Values are absolute path strings under UPLOAD_DIR.
uploaded_models_by_session: dict[str, set[str]] = {}
uploaded_models_lock = threading.Lock()


def _sanitize_upload_session_id(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    if not value:
        return None
    if len(value) > _MAX_UPLOAD_SESSION_ID_LEN:
        value = value[:_MAX_UPLOAD_SESSION_ID_LEN]
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if any(ch not in allowed for ch in value):
        return None
    return value


def _register_uploaded_model(session_id: str | None, model_path: Path) -> None:
    if not session_id:
        return
    resolved_upload_root = UPLOAD_DIR.resolve()
    resolved_model_path = model_path.resolve()
    try:
        resolved_model_path.relative_to(resolved_upload_root)
    except ValueError:
        return
    with uploaded_models_lock:
        session_models = uploaded_models_by_session.setdefault(session_id, set())
        session_models.add(str(resolved_model_path))


def _cleanup_uploaded_models_for_session(session_id: str | None) -> dict[str, Any]:
    if not session_id:
        return {"deleted": [], "errors": []}

    with uploaded_models_lock:
        tracked_models = uploaded_models_by_session.pop(session_id, set())

    if not tracked_models:
        return {"deleted": [], "errors": []}

    deleted: list[str] = []
    errors: list[str] = []
    upload_root = UPLOAD_DIR.resolve()
    for model_str in tracked_models:
        model_path = Path(model_str)
        try:
            resolved_model_path = model_path.resolve()
            resolved_model_path.relative_to(upload_root)
            if resolved_model_path.exists() and resolved_model_path.is_file():
                resolved_model_path.unlink()
                deleted.append(str(resolved_model_path))
        except Exception as exc:
            errors.append(f"{model_path}: {exc}")

    # Refresh in-memory model list and invalidate caches after cleanup.
    global AVAILABLE_MODELS, MODEL_NAME_LIST, last_render_fingerprint, last_render_response, renderers_by_model
    with models_lock:
        AVAILABLE_MODELS = _discover_models() or [DEFAULT_MODEL]
        MODEL_NAME_LIST = [p.stem for p in AVAILABLE_MODELS]
        renderers_by_model.clear()

    with state_lock:
        if state.current_model_index >= len(AVAILABLE_MODELS):
            state.current_model_index = 0
        last_render_fingerprint = None
        last_render_response = None

    with quantized_render_cache_lock:
        quantized_render_cache.clear()
    with preview_payload_cache_lock:
        preview_payload_cache.clear()

    return {"deleted": deleted, "errors": errors}


@app.route("/upload", methods=["POST"])
def upload_model():
    """Accept an STL or STEP file upload and add it to the model list."""
    global AVAILABLE_MODELS, MODEL_NAME_LIST, last_render_fingerprint, last_render_response, renderers_by_model

    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file field in request"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"status": "error", "message": "No file selected"}), 400

    upload_session_id = _sanitize_upload_session_id(
        request.form.get("upload_session_id") or request.headers.get("X-Upload-Session")
    )

    filename = secure_filename(file.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        return jsonify({"status": "error", "message": f"Unsupported file type '{suffix}'. Use .stl or .step"}), 400

    dest = UPLOAD_DIR / filename
    if dest.exists():
        stem = Path(filename).stem
        unique_name = f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"
        dest = UPLOAD_DIR / unique_name
        filename = unique_name
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        file.save(dest)
    except Exception as error:
        return jsonify(
            {
                "status": "error",
                "message": f"Could not save file in '{UPLOAD_DIR}': {error}",
            }
        ), 500

    with models_lock:
        AVAILABLE_MODELS = _discover_models() or [DEFAULT_MODEL]
        MODEL_NAME_LIST = [p.stem for p in AVAILABLE_MODELS]
        # Uploads can overwrite an existing filename or reorder the discovered
        # model list, so any renderer cached by index may now point at stale data.
        renderers_by_model.clear()
        new_index = next((i for i, p in enumerate(AVAILABLE_MODELS) if p == dest), 0)

    _register_uploaded_model(upload_session_id, dest)

    with state_lock:
        last_render_fingerprint = None
        last_render_response = None
    with quantized_render_cache_lock:
        quantized_render_cache.clear()
    with preview_payload_cache_lock:
        preview_payload_cache.clear()

    _log(f"Model uploaded: {filename} → index {new_index}", force=True)
    return jsonify({
        "status": "success",
        "filename": filename,
        "model_list": MODEL_NAME_LIST,
        "new_model_index": new_index,
    }), 200


@app.route("/uploads/cleanup", methods=["POST"])
def cleanup_uploaded_models():
    payload = request.get_json(silent=True) or {}
    upload_session_id = _sanitize_upload_session_id(
        payload.get("upload_session_id")
        or request.form.get("upload_session_id")
        or request.args.get("upload_session_id")
        or request.headers.get("X-Upload-Session")
    )
    if not upload_session_id:
        return jsonify({"status": "error", "message": "Missing upload_session_id"}), 400

    result = _cleanup_uploaded_models_for_session(upload_session_id)
    return jsonify(
        {
            "status": "success",
            "deleted_count": len(result["deleted"]),
            "error_count": len(result["errors"]),
        }
    ), 200


@app.route("/events", methods=["GET"])
def sse_events():
    """Server-Sent Events stream for hardware state changes (WitMotion IMU, Slider).

    Replaces 1-second polling for hardware input — events are pushed immediately
    when device state changes, reducing perceived latency from ~1000 ms to ~10 ms.
    """
    def generate():
        client_queue = _queue_module.Queue(maxsize=20)
        with _sse_clients_lock:
            _sse_clients.append(client_queue)
        try:
            # Send current state immediately on connect so the client is in sync.
            with state_lock:
                initial = {
                    "cube_value": state.cube_value,
                    "model_list": MODEL_NAME_LIST,
                }
            yield f"data: {json.dumps(initial)}\n\n"
            while True:
                try:
                    msg = client_queue.get(timeout=25)
                    yield msg
                except _queue_module.Empty:
                    yield ": heartbeat\n\n"  # Keep TCP alive through proxies
        finally:
            with _sse_clients_lock:
                if client_queue in _sse_clients:
                    _sse_clients.remove(client_queue)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/get_data", methods=["GET"])
def get_data():
    with state_lock:
        payload = {
            "status": "success",
            "cube_value": state.cube_value,
            "slider_value": state.slider_value,
            "current_model": state.current_model_index,
            "model_list": MODEL_NAME_LIST,
        }
    return jsonify(payload), 200


@app.route("/render/export-source", methods=["POST"])
def render_export_source():
    """Render a high-fidelity tactile source image for export workflows.

    This endpoint intentionally avoids sending anything to braille hardware.
    """
    try:
        params = request.get_json(silent=True) or {}
        merged_params = dict(DEFAULT_RENDER_PARAMS)
        merged_params.update(params)
        merged_params["view"] = str(merged_params.get("view", "")).lower()
        merged_params["print_view"] = False

        export_width = _coerce_positive_int(params.get("export_width", 1000), 1000)

        engine = get_or_create_renderer()

        with render_lock:
            original_screen_size = list(engine.screen_size)
            if not original_screen_size or original_screen_size[0] <= 0:
                original_screen_size = [96, 40]
            aspect_ratio = float(original_screen_size[1]) / float(original_screen_size[0])
            export_height = max(1, int(round(export_width * aspect_ratio)))
            engine.screen_size = [export_width, export_height]
            try:
                out_guard, err_guard = _renderer_stdio_guard()
                with out_guard, err_guard:
                    rendered = engine.render(merged_params)
            finally:
                engine.screen_size = original_screen_size

        tactile_payload = _to_braille_payload(rendered)
        response = {
            "status": "success",
            "message": "Export source render complete",
            "image_shape": list(tactile_payload.shape),
            "image_base64": _img_to_base64_png(tactile_payload),
            "export_width": export_width,
            "export_height": export_height,
        }
        return jsonify(response), 200
    except Exception as error:
        _log(f"Error rendering export source: {error}", force=True)
        return jsonify({"status": "error", "message": str(error)}), 400


@app.route("/render/preview", methods=["POST"])
def render_preview():
    """Render only the high-fidelity browser preview.

    This endpoint avoids braille-device work so the main tactile render can
    return immediately and the preview can be fetched afterward.
    """
    try:
        _refresh_model_list_if_stale()
        merged_params, model_index, is_pan_request, _fingerprint = _prepare_render_params(request.get_json(silent=True))
        preview_width = _coerce_positive_int(merged_params.get("preview_width", 800), 800)
        use_cache = not is_pan_request and merged_params.get("print_view") is not True
        preview_b64, preview_shape = _make_hifi_preview(
            merged_params,
            model_index,
            preview_width=preview_width,
            use_cache=use_cache,
        )
        return jsonify(
            {
                "status": "success",
                "render_preview_base64": preview_b64,
                "render_preview_shape": preview_shape,
            }
        ), 200
    except Exception as error:
        _log(f"Error rendering preview: {error}", force=True)
        return jsonify({"status": "error", "message": str(error)}), 400


@app.route("/render/dotpad-hex", methods=["POST"])
def render_dotpad_hex():
    """Return the current render as a DotPad-compatible hex string.

    The hex string can be passed directly to the DotPad Web SDK's
    ``displayGraphicData(hexString)`` method.
    """
    try:
        params = request.get_json(silent=True) or {}
        merged_params, model_index, is_pan_request, _fingerprint = _prepare_render_params(params)

        # Use device-reported cell grid if provided; fall back to DotPad 300A defaults.
        dotpad_cols = max(1, min(int(params.get("dotpad_cols", _DOTPAD_COLS)), 128))
        dotpad_rows = max(1, min(int(params.get("dotpad_rows", _DOTPAD_LINES)), 64))
        total_cells = dotpad_cols * dotpad_rows
        # Each braille cell is 2 px wide × 4 px tall.
        pixel_width  = dotpad_cols * 2
        pixel_height = dotpad_rows * 4

        use_cache = not is_pan_request and merged_params.get("print_view") is not True
        braille_payload = _get_braille_payload_at_size(
            merged_params,
            model_index=model_index,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            use_cache=use_cache,
        )
        cells = _pixels_to_braille_cells_dotpad(
            braille_payload, lines=dotpad_rows, cols=dotpad_cols,
        )
        cell_bytes = cells[:total_cells].ljust(total_cells, b"\x00")
        hex_string = cell_bytes.hex().upper()

        return jsonify({
            "status": "success",
            "dotpad_graphic_hex": hex_string,
            "cell_count": total_cells,
        }), 200
    except Exception as error:
        _log(f"Error rendering DotPad hex: {error}", force=True)
        return jsonify({"status": "error", "message": str(error)}), 400


@app.route("/static/js/<path:filename>", methods=["GET"])
def serve_static_js(filename):
    """Serve JavaScript files from the static/js directory."""
    response = send_file(REPO_ROOT / "static" / "js" / filename, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/static/css/<path:filename>", methods=["GET"])
def serve_static_css(filename):
    """Serve CSS files from the static/css directory."""
    response = send_file(REPO_ROOT / "static" / "css" / filename, mimetype="text/css")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


_ALGO_AXIS_TO_VIEW = {"z": "x-", "y": "y-", "x": "z+"}
_ALGO_AXIS_TO_DIM  = {"z": 0, "y": 1, "x": 2}
_FEATURE_PRIORITY  = {"tunnel": 10, "cavity": 8, "protrusion": 5, "crater": 4, "blob": 1}


def _section_label(covers: list[str]) -> str:
    kinds = sorted(
        set(fid.split("_")[0] for fid in covers),
        key=lambda k: -_FEATURE_PRIORITY.get(k, 0),
    )
    return " + ".join(kinds) if kinds else "section"


@app.route("/recommend_sections", methods=["POST"])
def recommend_sections_view():
    """Analyze a model and return recommended cross-section planes.

    POST body (JSON): { "current_model": <int> }
    Response: { "status", "model_index", "sections": [{view, depth, label, covers}], "analysis_ms" }
    """
    if not _CROSS_SECTION_ALGO_AVAILABLE:
        return jsonify({"status": "error", "message": _CROSS_SECTION_ALGO_ERROR}), 503

    try:
        _refresh_model_list_if_stale()
        data = request.get_json(silent=True) or {}
        model_index = _normalize_model_index(data.get("current_model"))
        model_path = AVAILABLE_MODELS[model_index]

        t0 = time.perf_counter()
        filled_grid, raw_grid = _load_voxel_grids(str(model_path))
        sections, _ = _recommend_sections(filled_grid, raw_grid=raw_grid)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        result = []
        for s in sections:
            N = filled_grid.shape[_ALGO_AXIS_TO_DIM[s.axis]]
            depth = round(s.coordinate / (N - 1) * 100) if N > 1 else 0
            result.append({
                "view": _ALGO_AXIS_TO_VIEW[s.axis],
                "depth": depth,
                "label": _section_label(s.covers),
                "covers": s.covers,
            })

        return jsonify({
            "status": "success",
            "model_index": model_index,
            "sections": result,
            "analysis_ms": elapsed_ms,
        }), 200
    except Exception as error:
        _log(f"Error recommending sections: {error}", force=True)
        return jsonify({"status": "error", "message": str(error)}), 400


def main() -> int:
    _log("Server starting on http://localhost:6969", force=True)
    _log(f"Model directory: {MODEL_DIR}", force=True)
    _log(f"Upload directory: {UPLOAD_DIR}", force=True)
    _log(f"Upload directory writable: {_is_writable_directory(UPLOAD_DIR)}", force=True)
    _log(f"Models found: {len(AVAILABLE_MODELS)}", force=True)
    _log("Endpoints: POST /render, POST /command, GET /get_data", force=True)
    _log(f"Braille send logs: {BRAILLE_LOG_PATH}", force=True)
    if QUIET_MODE:
        _log("Output mode: quiet (set SERVER_VERBOSE=1 for debug logs)", force=True)

    initialize_default_braille_render()
    start_optional_hardware_watchers()
    open_viewer_in_browser()

    _log("Ready.", force=True)
    # threaded=True lets /events (SSE) and /get_data respond concurrently while
    # a render is in progress; render_lock still serializes the renders themselves.
    app.run(debug=False, host="0.0.0.0", port=6969, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())