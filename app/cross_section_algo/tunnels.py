"""
tunnels.py
==========

Phase 5: locating tunnels (the beta_1 handles), not just counting them.

Phase 2 told us HOW MANY tunnels there are; this tells us WHERE each one is. The
idea is the curve skeleton: thinning a solid down to its 1-voxel-wide spine, with
a topology-preserving thinning, keeps the same tunnels, and each tunnel shows up
as a loop in the skeleton. So we:

  1. skeletonize the solid (skimage),
  2. turn the skeleton into a graph of junctions/endpoints joined by branches (skan),
  3. count the independent loops (the graph's cyclomatic number) -- this should
     match beta_1, and
  4. return the loops' voxel coordinates as the located tunnel loops.

Depends on `skan` and `networkx` on top of numpy/scikit-image.
"""

import numpy as np
import networkx as nx
from skimage.morphology import skeletonize
from skan import Skeleton, summarize


def _skeleton_and_summary(grid: np.ndarray):
    """Skeletonize and summarize, or (None, None) if there is nothing to trace."""
    skel = skeletonize(grid)
    if not skel.any():
        return None, None
    skeleton = Skeleton(skel)
    if skeleton.n_paths == 0:
        return None, None
    return skeleton, summarize(skeleton, separator="_")


def count_tunnel_loops(grid: np.ndarray) -> int:
    """Count independent tunnel loops via the skeleton's cyclomatic number.

    Build a multigraph whose nodes are the skeleton's junctions and endpoints and
    whose edges are the branches between them (a closed ring becomes a self-loop).
    The number of independent loops is then E - V + C (edges minus nodes plus
    connected components), which for a topology-preserving skeleton equals beta_1.
    """
    skeleton, summary = _skeleton_and_summary(grid)
    if skeleton is None:
        return 0

    graph = nx.MultiGraph()
    for src, dst in zip(summary["node_id_src"], summary["node_id_dst"]):
        graph.add_edge(int(src), int(dst))

    return (graph.number_of_edges()
            - graph.number_of_nodes()
            + nx.number_connected_components(graph))


def tunnel_loops(grid: np.ndarray, min_total_length: float = 10.0) -> list[np.ndarray]:
    """Return located tunnel loops as arrays of (z, y, x) voxel coordinates.

    Builds the complete skeleton branch MultiGraph (preserving parallel arcs and
    self-loops), finds a spanning forest via Union-Find, and yields one fundamental
    cycle per non-tree branch: the spanning-tree path between the branch's endpoints
    plus the branch itself.  This guarantees loop_count == E − V + C == b1.

    Loops whose total branch_distance is below min_total_length are discarded as
    skeletonization-noise wrinkles (physical-length floor, analogous to min_size
    for cavity detection).
    """
    skeleton, summary = _skeleton_and_summary(grid)
    if skeleton is None:
        return []

    src_ids = summary["node_id_src"].to_numpy().astype(int)
    dst_ids = summary["node_id_dst"].to_numpy().astype(int)
    lengths  = summary["branch_distance"].to_numpy()

    all_nodes = np.unique(np.concatenate([src_ids, dst_ids])).tolist()
    uf = {n: n for n in all_nodes}

    def _find(x):
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    def _union(x, y):
        rx, ry = _find(x), _find(y)
        if rx == ry:
            return False
        uf[rx] = ry
        return True

    tree_idx = set()
    non_tree  = []
    for i in np.argsort(lengths):
        i = int(i)
        s, d = src_ids[i], dst_ids[i]
        if s == d or not _union(s, d):
            non_tree.append(i)
        else:
            tree_idx.add(i)

    T = nx.Graph()
    for i in tree_idx:
        s, d = int(src_ids[i]), int(dst_ids[i])
        T.add_edge(s, d, path_idx=i, length=float(lengths[i]))

    loops = []
    for i in non_tree:
        s, d   = int(src_ids[i]), int(dst_ids[i])
        b_len  = float(lengths[i])

        if s == d:
            if b_len >= min_total_length:
                loops.append(np.asarray(skeleton.path_coordinates(i)))
            continue

        try:
            path_nodes = nx.shortest_path(T, s, d)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue

        tree_len = sum(
            T[path_nodes[j]][path_nodes[j + 1]]["length"]
            for j in range(len(path_nodes) - 1)
        )
        if tree_len + b_len < min_total_length:
            continue

        coords = []
        for j in range(len(path_nodes) - 1):
            u, v = path_nodes[j], path_nodes[j + 1]
            coords.extend(skeleton.path_coordinates(T[u][v]["path_idx"]))
        coords.extend(skeleton.path_coordinates(i))
        loops.append(np.asarray(coords))

    return loops
