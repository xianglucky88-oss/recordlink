"""匹配：阈值决策 + 冲突感知聚类（不按阈值简单连通成实体）。"""
from src.recordlink.config import load as load_config
from src.recordlink.match import cluster, decide_pairs
from src.recordlink.models import FieldScore, ScoreResult


def _sr(a, b, score, strong=False, unknown=()):
    return ScoreResult(a=a, b=b, score=score,
                       fields=[FieldScore("name", "match", score or 0.0)],
                       strong_conflict=strong, unknown_fields=list(unknown))


def test_threshold_bands():
    cfg = load_config()
    scored = [_sr("a", "b", 0.95), _sr("c", "d", 0.7), _sr("e", "f", 0.2),
              _sr("g", "h", None)]
    kinds = {(d.a, d.b): d.kind for d in decide_pairs(scored, cfg)}
    assert kinds[("a", "b")] == "auto_merge"
    assert kinds[("c", "d")] == "review"
    assert kinds[("e", "f")] == "no_merge"
    assert kinds[("g", "h")] == "review"      # 未知 => 待审而非不合并


def test_strong_conflict_never_auto_merges():
    cfg = load_config()
    scored = [_sr("a", "b", 0.97, strong=True)]
    (d,) = decide_pairs(scored, cfg)
    assert d.kind == "review" and d.reason == "strong_conflict"


def test_transitive_conflict_not_chained_into_one_entity():
    """A-B、B-C 相似但 A-C 强冲突：不得连成一个实体。"""
    cfg = load_config()
    scored = [_sr("a", "b", 0.95), _sr("b", "c", 0.94),
              _sr("a", "c", 0.6, strong=True)]
    decisions = decide_pairs(scored, cfg)
    clusters, review = cluster(decisions, scored, ["a", "b", "c"], cfg)
    memberships = sorted(tuple(sorted(c.members)) for c in clusters)
    assert ("a", "b", "c") not in memberships      # 没有连成单一实体
    blocked = [r for r in review if r["kind"] == "blocked_union"]
    assert blocked and set(blocked[0]["conflict_with"]) == {"a", "c"}


def test_one_to_many_kept_for_review():
    """X 同时与两个互不相似的实体相似：一对多歧义保留待审。"""
    cfg = load_config()
    scored = [_sr("x", "y", 0.9), _sr("x", "z", 0.9), _sr("y", "z", 0.3)]
    decisions = decide_pairs(scored, cfg)
    clusters, review = cluster(decisions, scored, ["x", "y", "z"], cfg)
    sizes = sorted(len(c.members) for c in clusters)
    assert sizes == [1, 2]                          # 只连成一对，另一个待审
    assert any(r["kind"] == "blocked_union" for r in review)


def test_unknown_cross_pair_allows_union_with_note():
    """A-B、B-C 相似，A-C 未被评分（未知）：允许合并（无已知负证据）。"""
    cfg = load_config()
    scored = [_sr("a", "b", 0.95), _sr("b", "c", 0.94)]
    decisions = decide_pairs(scored, cfg)
    clusters, review = cluster(decisions, scored, ["a", "b", "c"], cfg)
    memberships = [tuple(sorted(c.members)) for c in clusters]
    assert ("a", "b", "c") in memberships
