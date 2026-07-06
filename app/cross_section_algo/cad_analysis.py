"""
cad_analysis.py — voxelization driver (copied from algorithm repo).

Only load_voxel_grids is used by the CAD A11y integration; the rest of the
module is kept intact so the file stays in sync with the source repo.
"""

import numpy as np
import trimesh

from .topology import count_connected_components_fast, count_tunnels, count_cavities
from .morphology import classify_blobs, classify_craters

try:
    from .tunnels import tunnel_loops
    _HAVE_TUNNELS = True
except Exception:
    _HAVE_TUNNELS = False


def _mesh_to_grid(mesh, pitch: float = None,
                  resolution: int = 96, solid: bool = True) -> np.ndarray:
    if pitch is None:
        pitch = float(max(mesh.extents)) / resolution
    voxels = mesh.voxelized(pitch=pitch)
    if solid:
        voxels = voxels.fill()
    grid = np.asarray(voxels.matrix, dtype=bool)
    if min(grid.shape) < 3:
        raise ValueError(
            f"voxel grid {grid.shape} is too coarse — pitch is large relative "
            f"to the part. Increase resolution or lower pitch."
        )
    return grid


def load_voxel_grid(path: str, pitch: float = None,
                    resolution: int = 96, solid: bool = True) -> np.ndarray:
    mesh = trimesh.load(path, force="mesh")
    return _mesh_to_grid(mesh, pitch=pitch, resolution=resolution, solid=solid)


def load_voxel_grids(path: str, pitch: float = None,
                     resolution: int = 96) -> tuple:
    """Load a mesh and return (filled, raw) grids from one mesh load."""
    mesh = trimesh.load(path, force="mesh")
    filled = _mesh_to_grid(mesh, pitch=pitch, resolution=resolution, solid=True)
    raw = _mesh_to_grid(mesh, pitch=pitch, resolution=resolution, solid=False)
    return filled, raw


def analyze(grid: np.ndarray, crater_radius: int = None) -> dict:
    report = {
        "components_b0": count_connected_components_fast(grid),
        "tunnels_b1": count_tunnels(grid),
        "cavities_b2": count_cavities(grid),
        "blobs": classify_blobs(grid),
    }
    if _HAVE_TUNNELS:
        report["tunnel_loops"] = [
            tuple(round(float(c), 1) for c in loop.mean(axis=0))
            for loop in tunnel_loops(grid)
        ]
    if crater_radius is not None:
        report["craters"] = classify_craters(grid, bridge_radius=crater_radius)
    return report


def analyze_file(path: str, pitch: float = None, resolution: int = 96,
                 solid: bool = True, crater_radius: int = None) -> dict:
    grid = load_voxel_grid(path, pitch=pitch, resolution=resolution, solid=solid)
    return analyze(grid, crater_radius=crater_radius)


def analyze_bodies(path: str, pitch: float = None, resolution: int = 96,
                   solid: bool = True, crater_radius: int = None) -> list[dict]:
    mesh = trimesh.load(path, force="mesh")
    bodies = sorted(mesh.split(only_watertight=False),
                    key=lambda m: -float(np.prod(m.extents)))
    reports = []
    for body in bodies:
        grid = _mesh_to_grid(body, pitch=pitch, resolution=resolution, solid=solid)
        report = analyze(grid, crater_radius=crater_radius)
        report["extents"] = tuple(round(float(e), 4) for e in body.extents)
        reports.append(report)
    return reports
