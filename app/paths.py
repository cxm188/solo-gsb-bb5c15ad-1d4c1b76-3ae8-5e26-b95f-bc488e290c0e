"""依赖路径解释（POST /dependency-paths）：回答“目标包为什么会在锁定清单里”。

前置条件：``current_lock`` 已通过 :func:`app.audit.audit_lock` 审计（无
missing / unavailable / constraint_mismatch / extraneous）。此时锁中每个包
都可从根包按锁定版本的已激活依赖到达，且每个锁定版本都存在于快照中，
建图时不会再遇到断点。

建图与求路：
* 节点为锁中包（PEP 503 规范化名），边为锁定版本在目标环境下**已激活**
  的依赖（marker 在构造快照时已求值，与 /audit-lock 同一语义）；
* 从根包做 BFS，记录每个包的最短深度与全部最短前驱；菱形依赖的等长
  路径全部保留；
* 依赖环中每个节点只在首次（最短深度）入队一次，前驱深度沿路径严格
  递减，因此路径上不会重复节点，也不会无限遍历；
* 路径按节点序列字典序排序，结果与输入键序、候选列表顺序无关。
"""

from __future__ import annotations

from packaging.version import Version

from .audit import _find_candidate
from .repository import Repository, canonical


def dependency_paths(
    repo: Repository,
    *,
    root_name: str,
    current_lock: dict[str, Version],
    target_name: str,
) -> dict:
    """返回 ``{"reachable": bool, "paths": [[规范化包名, ...], ...]}``。

    ``paths`` 为从根包到目标包的全部最短包路径（按节点序列字典序排序）；
    目标为根包时是单节点路径；目标不在锁中或不可达时
    ``reachable=False`` 且 ``paths`` 为空列表。
    """
    root_name = canonical(root_name)
    target_name = canonical(target_name)

    if target_name == root_name:
        return {"reachable": True, "paths": [[root_name]]}
    if target_name not in current_lock:
        return {"reachable": False, "paths": []}

    def neighbours(name: str) -> list[str]:
        """该包锁定版本的已激活依赖（仅指向锁中包），去重并排序。"""
        cand = _find_candidate(repo, name, current_lock[name])
        if cand is None:  # 锁有效时不会发生；防御性兜底
            return []
        return sorted(
            {dep.name for dep in cand.deps if dep.name in current_lock}
        )

    # BFS：depth 记录最短深度，predecessors 记录全部最短前驱。
    depth = {root_name: 0}
    predecessors: dict[str, set[str]] = {}
    queue = [root_name]
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        if node == target_name:
            continue  # 最短路径不会以目标为中间节点，无需继续展开
        for nxt in neighbours(node):
            step = depth[node] + 1
            known = depth.get(nxt)
            if known is None:
                depth[nxt] = step
                predecessors[nxt] = {node}
                queue.append(nxt)
            elif known == step:
                # 菱形：等长的另一条入边同样是最短前驱。
                predecessors[nxt].add(node)
            # known < step：已有更短路径，忽略这条更长的入边（含环上回边）。

    if target_name not in depth:
        return {"reachable": False, "paths": []}

    # 迭代回溯枚举全部最短路径：前驱深度严格递减，环上不会重复节点。
    paths: list[list[str]] = []
    stack: list[tuple[str, list[str]]] = [(target_name, [])]
    while stack:
        node, suffix = stack.pop()
        if node == root_name:
            paths.append([node, *suffix])
            continue
        for pred in sorted(predecessors[node]):
            stack.append((pred, [node, *suffix]))
    paths.sort()
    return {"reachable": True, "paths": paths}
