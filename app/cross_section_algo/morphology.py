"""
morphology.py
=============

Phase 3: measuring the *shape* of an object, as opposed to Phase 2's topology.

Pieces so far:
  - distance_transform : the "depth landscape" (distance from each voxel to the surface)
  - fill_cavities      : fill enclosed voids so a blob reads as a solid lump
  - separate_blobs     : split touching lumps apart with a watershed on that landscape
  - count_blobs        : how many lumps the object splits into
"""

import numpy as np
from scipy import ndimage
from skimage.morphology import ball, h_maxima
from skimage.segmentation import watershed


def distance_transform(grid: np.ndarray) -> np.ndarray:
    """Depth landscape: distance from each filled voxel to the nearest empty one.

    Empty voxels are 0. A filled voxel touching the surface is about 1, and the
    value rises toward the object's core, peaking at roughly the local radius.

    Thin wrapper: scipy.ndimage.distance_transform_edt already computes, for each
    nonzero cell, the true Euclidean distance to the nearest zero cell. We give it
    a domain name so the rest of Phase 3 reads clearly.
    """
    return ndimage.distance_transform_edt(grid)


def fill_cavities(grid: np.ndarray) -> np.ndarray:
    """Return the object with every enclosed cavity filled in solid.

    Watershed assumes solid lumps. A void inside a blob piles the depth up in the
    surrounding shell instead of at a single core, which shatters one lump into
    many. Filling cavities first avoids that.

    scipy.ndimage.binary_fill_holes floods the background inward from the border
    and fills whatever it cannot reach -- the sealed voids. Its default flood is
    6-connected, which matches our cavity definition at 26-connectivity.
    """
    return ndimage.binary_fill_holes(grid)


def separate_blobs(grid: np.ndarray, min_persistence: float = 2.0) -> np.ndarray:
    """Split the object into lumps with a watershed on the depth landscape.

    Returns an integer label array: 0 is background (and any cavity interior),
    and each lump gets its own positive label.

    `min_persistence` is the significance threshold, in voxels of depth: a core
    is kept only if its summit is at least this much deeper than the saddle where
    it would merge into a deeper core. Shallower peaks (surface noise, flat-ridge
    plateaus) are merged away instead of seeding spurious blobs.
    """
    # Guard: fill cavities so each lump reads as solid before we measure depth.
    solid = fill_cavities(grid)

    depth = distance_transform(solid)

    # h_maxima keeps only the maxima whose persistence is at least min_persistence
    # and returns them as a binary mask, with each surviving core a connected
    # region. A flat ridge collapses to one region instead of many points.
    significant_cores = h_maxima(depth, min_persistence)

    # Turn each connected core region into one numbered watershed marker.
    markers, n_markers = ndimage.label(significant_cores)

    # Robustness: guarantee every connected piece gets at least one core. A piece
    # too thin to clear the persistence threshold would otherwise get no marker
    # and vanish from the result, so we seed any uncovered piece at its deepest
    # voxel.
    full_neighborhood = ndimage.generate_binary_structure(3, 3)
    components, n_components = ndimage.label(solid, structure=full_neighborhood)
    for component_id in range(1, n_components + 1):
        piece = components == component_id
        if not np.any(markers[piece]):
            piece_depth = np.where(piece, depth, -1.0)
            deepest = np.unravel_index(np.argmax(piece_depth), piece_depth.shape)
            n_markers += 1
            markers[deepest] = n_markers

    # Flood the inverted depth so deep cores act as basins; the basins meet along
    # the cuts between blobs.
    labels = watershed(-depth, markers, mask=solid)

    # Label only the real material: drop background and any filled-in cavities.
    labels[~grid] = 0
    return labels


def count_blobs(grid: np.ndarray, min_persistence: float = 2.0) -> int:
    """How many separate lumps the object splits into."""
    labels = separate_blobs(grid, min_persistence)
    # Count the distinct positive labels (0 is background).
    return len(np.unique(labels[labels > 0]))


def axis_lengths(blob_mask: np.ndarray) -> np.ndarray:
    """The three principal axis lengths of a single blob, sorted a >= b >= c.

    We treat each voxel as a point and look at how the blob spreads in space: the
    covariance matrix of the voxel coordinates. Its eigenvalues are the variances
    along the blob's natural (principal) axes, and the square root of each is
    proportional to that axis's half-length. Since classification uses only the
    ratios between lengths, the common proportionality constant cancels out.
    """
    coords = np.argwhere(blob_mask)            # (N, 3) array of (z, y, x) voxels

    # Covariance of the coordinates (rowvar=False: each column is one axis).
    covariance = np.cov(coords, rowvar=False)

    # eigvalsh returns the eigenvalues of a symmetric matrix in ascending order.
    variances = np.linalg.eigvalsh(covariance)

    lengths = np.sqrt(variances)               # half-length is proportional to sqrt(variance)
    return np.sort(lengths)[::-1]              # sort descending: a >= b >= c


def classify_shape(blob_mask: np.ndarray, tolerance: float = 0.85) -> str:
    """Classify a blob's shape from its axis-length ratios.

    Returns one of 'sphere', 'oblate', 'prolate', 'triaxial':
        sphere   a ~ b ~ c   (all three similar)
        oblate   a ~ b > c   (a flattened disc: two long axes, one short)
        prolate  a > b ~ c   (a cigar: one long axis, two short)
        triaxial a > b > c   (all three different)
    Two axes count as similar when the shorter is at least `tolerance` times the
    longer (default 0.85, i.e. within 15%).
    """
    a, b, c = axis_lengths(blob_mask)
    top_two_similar = (b / a) >= tolerance
    bottom_two_similar = (c / b) >= tolerance

    if top_two_similar and bottom_two_similar:
        return "sphere"
    if top_two_similar:                        # a ~ b, but c is smaller
        return "oblate"
    if bottom_two_similar:                      # b ~ c, but a is larger
        return "prolate"
    return "triaxial"


def classify_blobs(grid: np.ndarray, min_persistence: float = 2.0,
                   tolerance: float = 0.85) -> dict[int, str]:
    """Separate the object into blobs and classify each one's shape.

    Returns a dict mapping each blob's label to its shape name -- the capstone
    that runs the whole Phase 3 pipeline: fill cavities, find cores, watershed,
    then measure and classify each resulting lump.
    """
    labels = separate_blobs(grid, min_persistence)

    shapes = {}
    for label in np.unique(labels[labels > 0]):
        blob_mask = labels == label
        shapes[int(label)] = classify_shape(blob_mask, tolerance)
    return shapes


def detect_craters(grid: np.ndarray, bridge_radius: int = 8,
                   min_size: int = 20) -> np.ndarray:
    """Return a mask of concave surface pockets (craters).

    We "bridge" over surface dents with a morphological closing: imagine rolling
    a ball of radius `bridge_radius` across the surface -- it spans the mouth of a
    pocket without dropping in. The material the bridging adds back, minus the
    original object, is the set of filled-in pockets. Connected pockets smaller
    than `min_size` voxels are dropped as discretization noise.

    `bridge_radius` sets the scale: a pocket whose mouth is wider than the ball is
    not bridged (the ball falls in), so increase it to catch wider craters. A
    crater is purely a surface feature -- it does not change any Betti number,
    which is what distinguishes it from a tunnel or a cavity.
    """
    # Pad so the closing's dilation/erosion never run into the grid border.
    pad = bridge_radius + 1
    padded = np.pad(grid, pad)

    # Closing = dilate then erode with a ball; it fills concavities up to the
    # ball's scale. Subtracting the original leaves just the filled-in pockets.
    bridged = ndimage.binary_closing(padded, structure=ball(bridge_radius))
    pockets = (bridged & ~padded)[pad:-pad, pad:-pad, pad:-pad]   # undo the padding

    # Drop tiny specks from surface stair-stepping; keep the real pockets.
    labels, n = ndimage.label(pockets)
    sizes = np.bincount(labels.ravel())
    keep = sizes >= min_size
    keep[0] = False                      # label 0 is background
    return keep[labels]                  # map each voxel's label to keep/discard


def count_craters(grid: np.ndarray, bridge_radius: int = 8,
                  min_size: int = 20) -> int:
    """Count the concave surface pockets (craters) on the object."""
    _, n = ndimage.label(detect_craters(grid, bridge_radius, min_size))
    return n


def classify_crater_shape(crater_mask: np.ndarray, tolerance: float = 0.85) -> str:
    """Classify a single crater as a 'round pit' or an 'elongated gully'.

    Uses the same principal axis lengths as blob classification (a >= b >= c).
    A crater's two larger axes (a, b) describe its mouth and the smallest (c) is
    its depth. If the mouth axes are similar the pocket is roughly circular -- a
    round pit -- and if a is clearly longer than b the mouth is stretched into a
    gully. "Similar" means the shorter is at least `tolerance` times the longer.
    """
    a, b, _ = axis_lengths(crater_mask)
    if (b / a) >= tolerance:
        return "round pit"
    return "elongated gully"


def classify_craters(grid: np.ndarray, bridge_radius: int = 8,
                     min_size: int = 20, tolerance: float = 0.85) -> dict[int, str]:
    """Detect every crater and classify each one's shape.

    Returns a dict mapping each crater's label to 'round pit' or 'elongated gully'.
    """
    labels, n = ndimage.label(detect_craters(grid, bridge_radius, min_size))

    shapes = {}
    for label in range(1, n + 1):
        shapes[label] = classify_crater_shape(labels == label, tolerance)
    return shapes


def detect_protrusions(grid: np.ndarray, bump_radius: int = 5,
                       min_size: int = 20) -> np.ndarray:
    """Return a mask of convex bumps sticking out of the solid (studs, pins, nubs).

    The mirror of detect_craters. Where craters use a closing to fill concave
    pockets, protrusions use a morphological OPENING: rolling a ball of radius
    `bump_radius` INSIDE the solid erases the thin parts it cannot fit into -- the
    bits sticking out. Subtracting the opened solid from the original leaves those
    bumps. Connected bumps smaller than `min_size` voxels are dropped as noise.

    `bump_radius` sets the scale: a feature wider than the ball survives the
    opening (not a protrusion), so raise it to keep only the more slender bumps.
    Like craters, this is a surface feature -- it changes no Betti number.
    """
    # Pad so the opening's erosion/dilation never run into the grid border.
    pad = bump_radius + 1
    padded = np.pad(grid, pad)

    # Opening = erode then dilate with a ball; it strips off thin protrusions.
    opened = ndimage.binary_opening(padded, structure=ball(bump_radius))
    bumps = (padded & ~opened)[pad:-pad, pad:-pad, pad:-pad]   # undo the padding

    # Drop tiny specks; keep the real bumps.
    labels, n = ndimage.label(bumps)
    sizes = np.bincount(labels.ravel())
    keep = sizes >= min_size
    keep[0] = False                      # label 0 is background
    return keep[labels]


def count_protrusions(grid: np.ndarray, bump_radius: int = 5,
                      min_size: int = 20) -> int:
    """Count the convex bumps (protrusions) on the object."""
    _, n = ndimage.label(detect_protrusions(grid, bump_radius, min_size))
    return n


def classify_protrusion_shape(protrusion_mask: np.ndarray, tolerance: float = 0.85) -> str:
    """Classify a single protrusion as a 'round nub' or an elongated 'ridge'.

    Same principal-axis idea as blobs and craters: if the two larger axes are
    similar the bump is roughly round, otherwise it is stretched into a ridge.
    """
    a, b, _ = axis_lengths(protrusion_mask)
    if (b / a) >= tolerance:
        return "round nub"
    return "ridge"


def classify_protrusions(grid: np.ndarray, bump_radius: int = 5,
                         min_size: int = 20, tolerance: float = 0.85) -> dict[int, str]:
    """Detect every protrusion and classify each one's shape."""
    labels, n = ndimage.label(detect_protrusions(grid, bump_radius, min_size))

    shapes = {}
    for label in range(1, n + 1):
        shapes[label] = classify_protrusion_shape(labels == label, tolerance)
    return shapes