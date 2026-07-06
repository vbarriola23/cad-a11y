"""
cross_sections.py — greedy cross-section planner (copied from algorithm repo).

Internal imports converted to relative for use as a package inside CAD A11y.
"""

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .morphology import detect_craters, detect_protrusions
from .topology import _dual_connectivity, _flood_from_border

try:
    from .tunnels import tunnel_loops
except Exception as _skan_err:
    raise ImportError(
        "Tunnel detection unavailable: could not import skan (needed for "
        "skeleton-graph loop detection). Check that skan and a compatible "
        f"numba are installed. Original error: {_skan_err}"
    ) from _skan_err


@dataclass
class Feature:
    id: str
    kind: str
    coords: np.ndarray
    centroid: np.ndarray
    bbox_min: np.ndarray
    bbox_max: np.ndarray

    @property
    def voxel_count(self) -> int:
        return len(self.coords)

    @property
    def bbox_size(self) -> np.ndarray:
        return self.bbox_max - self.bbox_min + 1


@dataclass
class Section:
    axis: str
    coordinate: int
    covers: list[str]


def _feature_from_mask(mask: np.ndarray, feature_id: str, kind: str) -> Feature:
    coords = np.argwhere(mask)
    return Feature(
        id=feature_id, kind=kind, coords=coords,
        centroid=coords.mean(axis=0),
        bbox_min=coords.min(axis=0), bbox_max=coords.max(axis=0),
    )


def _feature_from_coords(coords: np.ndarray, feature_id: str, kind: str) -> Feature:
    coords = np.asarray(coords)
    return Feature(
        id=feature_id, kind=kind, coords=coords,
        centroid=coords.mean(axis=0),
        bbox_min=coords.min(axis=0), bbox_max=coords.max(axis=0),
    )


def locate_cavities(grid: np.ndarray, connectivity: int = 26, min_size: int = 3) -> list[Feature]:
    padded = np.pad(grid, 1, constant_values=False)
    background = ~padded
    ext_conn = _dual_connectivity(connectivity)
    exterior = _flood_from_border(background, ext_conn)
    trapped = background & ~exterior

    scipy_conn = {6: 1, 26: 3}[ext_conn]
    structure = ndimage.generate_binary_structure(3, scipy_conn)
    labels, n = ndimage.label(trapped, structure=structure)

    outer_surface = padded & ndimage.binary_dilation(exterior, structure=structure)

    features = []
    for label in range(1, n + 1):
        mask = labels == label
        if int(mask.sum()) < min_size:
            continue
        boundary = ndimage.binary_dilation(mask, structure=structure) & padded
        if np.any(boundary & outer_surface):
            continue
        coords = np.argwhere(mask) - 1
        features.append(_feature_from_coords(coords, f"cavity_{label}", "cavity"))
    return features


def locate_blobs(grid: np.ndarray) -> list[Feature]:
    structure = ndimage.generate_binary_structure(3, 3)
    labels, _ = ndimage.label(grid, structure=structure)
    features = []
    for label in np.unique(labels):
        if label == 0:
            continue
        features.append(_feature_from_mask(labels == label, f"blob_{label}", "blob"))
    return features


def locate_protrusions(
    grid: np.ndarray, bump_radius: int = 5, min_size: int = 20,
    max_fraction: float = 0.1
) -> list[Feature]:
    mask = detect_protrusions(grid, bump_radius=bump_radius, min_size=min_size)
    labels, n = ndimage.label(mask)
    total_voxels = int(grid.sum())
    max_voxels = total_voxels * max_fraction
    features = []
    for label in range(1, n + 1):
        feature = _feature_from_mask(labels == label, f"protrusion_{label}", "protrusion")
        if feature.voxel_count <= max_voxels:
            features.append(feature)
    return features


def locate_craters(
    grid: np.ndarray, bridge_radius: int = 8, min_size: int = 20
) -> list[Feature]:
    crater_mask = detect_craters(grid, bridge_radius=bridge_radius, min_size=min_size)
    labels, n = ndimage.label(crater_mask)
    features = []
    for label in range(1, n + 1):
        features.append(_feature_from_mask(labels == label, f"crater_{label}", "crater"))
    return features


def locate_tunnels(grid: np.ndarray) -> list[Feature]:
    loops = tunnel_loops(grid)
    features = []
    for i, loop in enumerate(loops, start=1):
        loop = np.asarray(loop)
        features.append(Feature(
            id=f"tunnel_{i}", kind="tunnel", coords=loop,
            centroid=loop.mean(axis=0),
            bbox_min=loop.min(axis=0), bbox_max=loop.max(axis=0),
        ))
    return features


def extract_features(
    grid: np.ndarray, crater_radius: int | None = None, bump_radius: int = 5,
    raw_grid: np.ndarray | None = None
) -> list[Feature]:
    cavity_grid = ndimage.binary_closing(raw_grid, iterations=1) if raw_grid is not None else grid
    features = []
    features.extend(locate_blobs(grid))
    features.extend(locate_cavities(cavity_grid))
    features.extend(locate_tunnels(grid))
    features.extend(locate_protrusions(grid, bump_radius=bump_radius))
    if crater_radius is not None:
        features.extend(locate_craters(grid, bridge_radius=crater_radius))
    return features


DEFAULT_WEIGHTS = {
    "tunnel": 10, "cavity": 8, "protrusion": 5, "crater": 4, "blob": 1,
}

_AXIS_IDX = {"z": 0, "y": 1, "x": 2}


def generate_candidate_planes(features):
    candidates = set()
    for feature in features:
        z, y, x = feature.centroid
        candidates.add(("x", int(round(x))))
        candidates.add(("y", int(round(y))))
        candidates.add(("z", int(round(z))))
    return sorted(candidates)


def plane_intersects_feature(axis: str, coordinate: int, feature: Feature) -> bool:
    coords = feature.coords
    if axis == "x":
        return np.any(coords[:, 2] == coordinate)
    if axis == "y":
        return np.any(coords[:, 1] == coordinate)
    if axis == "z":
        return np.any(coords[:, 0] == coordinate)
    raise ValueError(axis)


def build_coverage_table(features, candidates):
    coverage = {}
    for axis, coordinate in candidates:
        covered = set()
        for feature in features:
            if plane_intersects_feature(axis, coordinate, feature):
                covered.add(feature.id)
        coverage[(axis, coordinate)] = covered
    return coverage


def _missing_blob_axis_sections(selected: list, blobs: list, coverage: dict) -> list[tuple]:
    covered_axes = {axis for axis, _ in selected}
    seen = set(selected)
    extra = []
    for blob in blobs:
        for axis in ["z", "y", "x"]:
            if axis not in covered_axes:
                coord = int(round(blob.centroid[_AXIS_IDX[axis]]))
                plane = (axis, coord)
                if plane in coverage and plane not in seen:
                    extra.append(plane)
                    seen.add(plane)
                    covered_axes.add(axis)
    return extra


def _plan_sections(features: list, weights: dict | None = None) -> list:
    weights = weights or DEFAULT_WEIGHTS
    if not features:
        return []

    candidates = generate_candidate_planes(features)
    coverage = build_coverage_table(features, candidates)
    feature_lookup = {f.id: f for f in features}
    uncovered = {f.id for f in features}

    selected = []
    while uncovered:
        best_plane = None
        best_score = -1
        for plane, covered in coverage.items():
            new_features = covered & uncovered
            score = sum(weights.get(feature_lookup[fid].kind, 1) for fid in new_features)
            if score > best_score:
                best_score = score
                best_plane = plane
        if best_plane is None:
            break
        selected.append(best_plane)
        uncovered -= coverage[best_plane]

    blobs = [f for f in features if f.kind == "blob"]
    selected.extend(_missing_blob_axis_sections(selected, blobs, coverage))

    return [
        Section(axis=axis, coordinate=coordinate, covers=sorted(coverage[(axis, coordinate)]))
        for axis, coordinate in selected
    ]


def recommend_sections(
    grid: np.ndarray,
    crater_radius: int | None = None,
    bump_radius: int = 5,
    weights=None,
    raw_grid: np.ndarray | None = None,
):
    """Plan a covering set of section planes. Returns (sections, features)."""
    features = extract_features(grid, crater_radius=crater_radius,
                                bump_radius=bump_radius, raw_grid=raw_grid)
    if not features:
        return [], []
    return _plan_sections(features, weights=weights), features
