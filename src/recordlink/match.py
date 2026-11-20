"""单一职责：匹配决策与实体聚类。

- 强冲突 => 永不自动合并（分数再高也只进待审或不合并）；
- 无共同已知证据（score 为 None）=> 未知，进待审而不是当作不相似；
- 聚类不是“超过阈值就连通”：合并两个分量前检查所有跨分量已知证据，
  存在强冲突或已知不相似则阻断，并把歧义（一对多、A-B/B-C 相似但 A-C 冲突）
  保留为待审，而不是硬连成一个实体。
"""
import hashlib

from .models import Cluster, Decision


def decide_pairs(scored, config):
    decisions = []
    for s in scored:
        if s.strong_conflict:
            if s.score is not None and s.score >= config.review_threshold:
                decisions.append(Decision(s.a, s.b, "review", "strong_conflict", s.score))
            else:
                decisions.append(Decision(s.a, s.b, "no_merge", "strong_conflict", s.score))
        elif s.score is None:
            decisions.append(Decision(s.a, s.b, "review", "unknown_evidence", None))
        elif s.score >= config.auto_merge_threshold:
            decisions.append(Decision(s.a, s.b, "auto_merge", "score>=auto_threshold", s.score))
        elif s.score >= config.review_threshold:
            decisions.append(Decision(s.a, s.b, "review", "score_in_review_band", s.score))
        else:
            decisions.append(Decision(s.a, s.b, "no_merge", "score_below_review", s.score))
    return decisions


def cluster_id_of(members):
    digest = hashlib.sha256(",".join(sorted(members)).encode()).hexdigest()[:12]
    return f"cl_{digest}"


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def members(self, root):
        root = self.find(root)
        return sorted(k for k in self.parent if self.find(k) == root)


def cluster(decisions, scored, all_keys, config):
    """返回 (clusters, review_items)。

    只对 auto_merge 边做并查集合并，且每次合并前做一致性检查：
    跨分量的任意已知证据若为强冲突或已知不相似，则阻断该合并并记入待审。
    这样 A-B、B-C 相似但 A-C 冲突时不会连成一个实体，一对多歧义保留待审。
    """
    index = {(s.a, s.b): s for s in scored}
    index.update({(s.b, s.a): s for s in scored})

    def negative_evidence(members_a, members_b):
        for x in members_a:
            for y in members_b:
                s = index.get((x, y))
                if s is None:
                    continue  # 未评分的跨桶对：未知，不阻断，但合并边本身有评分
                if s.strong_conflict:
                    return (x, y, "strong_conflict", s.score)
                if s.score is not None and s.score < config.review_threshold:
                    return (x, y, "known_dissimilar", s.score)
        return None

    uf = _UnionFind()
    for k in all_keys:
        uf.find(k)

    review = []
    edges = sorted((d for d in decisions if d.kind == "auto_merge"),
                   key=lambda d: -(d.score or 0))
    accepted = []
    for d in edges:
        ra, rb = uf.find(d.a), uf.find(d.b)
        if ra == rb:
            accepted.append(d)
            continue
        bad = negative_evidence(uf.members(ra), uf.members(rb))
        if bad:
            review.append({
                "kind": "blocked_union", "a": d.a, "b": d.b,
                "score": d.score, "conflict_with": [bad[0], bad[1]],
                "reason": f"合并被阻断：{bad[0]} 与 {bad[1]} 存在 {bad[2]}"
                          f"（一对多/传递冲突，不连成同一实体，保留人工待审）",
            })
            continue
        uf.union(ra, rb)
        accepted.append(d)

    groups = {}
    for k in all_keys:
        groups.setdefault(uf.find(k), []).append(k)

    clusters = []
    for members in sorted(groups.values()):
        cid = cluster_id_of(members)
        edges_in = [d for d in accepted
                    if uf.find(d.a) == uf.find(members[0]) and len(members) > 1]
        clusters.append(Cluster(cluster_id=cid, members=sorted(members), edges=edges_in))

    for d in decisions:
        if d.kind == "review":
            review.append({"kind": "pair_review", "a": d.a, "b": d.b,
                           "score": d.score, "reason": d.reason})
    return clusters, review


def run_stage(ctx):
    ctx.decisions = decide_pairs(ctx.scored, ctx.config)
    all_keys = [r["_key"] for r in ctx.records]
    ctx.clusters, ctx.review = cluster(ctx.decisions, ctx.scored, all_keys, ctx.config)
    # 匹配层待审项：先收集进 ctx，发布代次时统一落库
    for item in ctx.review:
        if item["kind"] == "pair_review":
            item_id = f"rev_pair_{item['a']}_{item['b']}"
        else:
            item_id = f"rev_blocked_{item['a']}_{item['b']}"
        ctx.pending_items.append({"item_id": item_id, "kind": "pair", "data": item})
    return ctx
