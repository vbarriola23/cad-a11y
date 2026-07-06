"""
topology.py
===========

Phase 2: counting the topological features of a voxel object.

    beta_0 = number of connected components (separate pieces)
    beta_1 = number of tunnels / through-holes
    beta_2 = number of cavities (fully enclosed voids)

Connectivity: in a 3D grid you must choose which voxels count as neighbors, and
the two valid (dual) choices are 6 (faces only) and 26 (faces, edges, corners).
We pass it in explicitly and work at 26 for tunnels, to match skimage's Euler
characteristic (its connectivity=3).
"""

from collections import deque

import numpy as np
from scipy import ndimage
from skimage.measure import euler_number


def _neighbor_offsets(connectivity: int) -> list[tuple[int, int, int]]:
    """Return the (dz, dy, dx) steps that reach a voxel's neighbors."""
    if connectivity == 6:
        # The six face neighbors: one step along a single axis.
        return [(-1, 0, 0), (1, 0, 0),
                (0, -1, 0), (0, 1, 0),
                (0, 0, -1), (0, 0, 1)]

    if connectivity == 26:
        # Everything in the surrounding 3x3x3 block except the voxel itself.
        return [(dz, dy, dx)
                for dz in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for dx in (-1, 0, 1)
                if not (dz == 0 and dy == 0 and dx == 0)]

    raise ValueError(f"connectivity must be 6 or 26, got {connectivity}")


def count_connected_components(grid: np.ndarray, connectivity: int = 26) -> int:
    """beta_0: the number of separate filled pieces, via flood fill.

    Scan for a filled voxel we have not claimed yet; it starts a new component,
    which we flood outward to claim before moving on.
    """
    offsets = _neighbor_offsets(connectivity)
    depth, height, width = grid.shape
    visited = np.zeros_like(grid)         # all False; marks voxels already claimed
    component_count = 0

    # np.argwhere(grid) lists every filled voxel as (z, y, x).
    for z, y, x in np.argwhere(grid):
        if visited[z, y, x]:
            continue

        # A fresh, unclaimed voxel begins a new component.
        component_count += 1
        visited[z, y, x] = True

        # Breadth-first flood fill from this seed (deque = fast FIFO queue).
        queue = deque([(z, y, x)])
        while queue:
            vz, vy, vx = queue.popleft()
            for dz, dy, dx in offsets:
                nz, ny, nx = vz + dz, vy + dy, vx + dx
                inside_grid = (0 <= nz < depth) and (0 <= ny < height) and (0 <= nx < width)
                if inside_grid and grid[nz, ny, nx] and not visited[nz, ny, nx]:
                    visited[nz, ny, nx] = True
                    queue.append((nz, ny, nx))

    return component_count


def count_connected_components_fast(grid: np.ndarray, connectivity: int = 26) -> int:
    """beta_0 via SciPy's optimized labeling -- used to cross-check the version above."""
    # SciPy's own scale: 1 = faces, 3 = faces+edges+corners.
    scipy_connectivity = {6: 1, 26: 3}[connectivity]
    structure = ndimage.generate_binary_structure(rank=3, connectivity=scipy_connectivity)
    _labeled, num_components = ndimage.label(grid, structure=structure)
    return num_components


def _dual_connectivity(connectivity: int) -> int:
    """The connectivity the background must use to stay consistent (6 <-> 26)."""
    return {6: 26, 26: 6}[connectivity]


def _flood_from_border(mask: np.ndarray, connectivity: int) -> np.ndarray:
    """Return the cells of `mask` reachable from the grid's outer border.

    Multi-source flood fill: seed from every border cell at once and spread inward.
    """
    offsets = _neighbor_offsets(connectivity)
    depth, height, width = mask.shape
    reached = np.zeros_like(mask)

    # Mark the six outer faces of the box, then seed from the mask cells on them.
    border = np.zeros_like(mask)
    border[[0, -1], :, :] = True
    border[:, [0, -1], :] = True
    border[:, :, [0, -1]] = True

    queue = deque()
    for z, y, x in np.argwhere(mask & border):
        reached[z, y, x] = True
        queue.append((z, y, x))

    # Spread inward from all seeds simultaneously.
    while queue:
        vz, vy, vx = queue.popleft()
        for dz, dy, dx in offsets:
            nz, ny, nx = vz + dz, vy + dy, vx + dx
            inside_grid = (0 <= nz < depth) and (0 <= ny < height) and (0 <= nx < width)
            if inside_grid and mask[nz, ny, nx] and not reached[nz, ny, nx]:
                reached[nz, ny, nx] = True
                queue.append((nz, ny, nx))

    return reached


def count_cavities(grid: np.ndarray, connectivity: int = 26) -> int:
    """beta_2: empty voids fully enclosed by the object.

    `connectivity` is the FOREGROUND convention; the background uses its dual.
    """
    # Pad with a one-voxel empty border so "the outside" is always one connected
    # region, even if the object touches the original edge.
    padded = np.pad(grid, pad_width=1, mode="constant", constant_values=False)

    background = ~padded                                   # the empty cells
    background_connectivity = _dual_connectivity(connectivity)

    exterior = _flood_from_border(background, background_connectivity)
    trapped = background & ~exterior                       # empty cells the flood never reached

    # Each separate trapped pocket is one cavity.
    return count_connected_components(trapped, background_connectivity)


def count_tunnels(grid: np.ndarray, connectivity: int = 26) -> int:
    """beta_1: tunnels, via the identity chi = beta_0 - beta_1 + beta_2.

    Rearranged: beta_1 = beta_0 + beta_2 - chi. All three must use one
    connectivity convention; we use 26, which is skimage's connectivity=3.
    """
    if connectivity != 26:
        raise ValueError("count_tunnels supports connectivity=26 only, to match "
                         "the Euler characteristic convention")

    beta_0 = count_connected_components(grid, connectivity=26)
    beta_2 = count_cavities(grid, connectivity=26)

    # skimage.measure.euler_number returns the Euler characteristic of the binary
    # volume; connectivity=3 is 26-connectivity, matching beta_0 and beta_2.
    chi = euler_number(grid, connectivity=3)

    return beta_0 + beta_2 - chi