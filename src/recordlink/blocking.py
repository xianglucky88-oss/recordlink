"""单一职责：候选生成（blocking）。

按配置的分桶规则把记录分桶，只在同桶内展开成对候选，控制计算成本。
报告中明确说明：
- 覆盖范围：哪些记录产生了候选、哪些记录因缺字段或桶唯一而未覆盖；
- 计算成本：实际展开的成对比较数、因超大桶被跳过的比较数。
"""
import re
from itertools import combinations

from .models import CandidateReport


def _norm_text(v):
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _name_prefix(rec):
    name = _norm_text(rec.get("name"))
    return f"np:{name[:3]}" if name else None


def _status_key(rec):
    st = _norm_text(rec.get("status"))
    return f"st:{st}" if st else None


def _value_bucket(rec):
    v = rec.get("value")
    if "value" in rec.get("_missing", []):
        return None
    try:
        return f"vb:{int(float(v)) // 100}"
    except (TypeError, ValueError):
        return None


KEY_FUNCS = {
    "name_prefix": _name_prefix,
    "status": _status_key,
    "value_bucket": _value_bucket,
}


def generate_candidates(records, config):
    """records: 含 _key 的规范化记录。返回 CandidateReport。"""
    for rule in config.blocking_keys:
        if rule not in KEY_FUNCS:
            raise ValueError(f"未知的分桶规则: {rule}（可选: {sorted(KEY_FUNCS)}）")

    buckets = {}
    missing = {}
    for rec in records:
        key = rec["_key"]
        for rule in config.blocking_keys:
            bkey = KEY_FUNCS[rule](rec)
            if bkey is None:
                missing.setdefault(rule, []).append(key)
                continue
            buckets.setdefault((rule, bkey), []).append(key)

    pairs, covered = set(), set()
    oversized, notes = [], []
    cost = skipped = 0
    for (rule, bkey), members in sorted(buckets.items()):
        n = len(members)
        if n < 2:
            continue
        if n > config.max_bucket_size:
            skip = n * (n - 1) // 2
            skipped += skip
            oversized.append({"rule": rule, "bucket": bkey, "size": n,
                              "skipped_pairs": skip})
            notes.append(
                f"桶 {bkey} 过大（{n} 条 > 上限 {config.max_bucket_size}），"
                f"跳过 {skip} 对比较以控制计算成本；这些记录可能漏合并，"
                f"需补充分桶规则或提高上限后重跑")
            continue
        cost += n * (n - 1) // 2
        covered.update(members)
        for a, b in combinations(sorted(members), 2):
            pairs.add((a, b))

    all_keys = [r["_key"] for r in records]
    uncovered = [k for k in all_keys if k not in covered]
    if missing:
        for rule, keys in sorted(missing.items()):
            notes.append(f"规则 {rule}: {len(keys)} 条记录缺字段，未进入该规则分桶: {keys}")
    if uncovered:
        notes.append(
            f"{len(uncovered)} 条记录未产生任何候选（缺字段或桶内无邻居）: {uncovered}；"
            f"它们不参与成对评分，按单记录实体处理")

    total = len(all_keys)
    return CandidateReport(
        pairs=sorted(pairs),
        estimated_comparisons=cost,
        skipped_comparisons=skipped,
        coverage=(len(covered) / total) if total else 1.0,
        covered=sorted(covered),
        uncovered=uncovered,
        oversized=oversized,
        missing_fields={k: sorted(v) for k, v in sorted(missing.items())},
        notes=notes,
    )


def run_stage(ctx):
    """流水线阶段：对活跃记录生成候选对，报告写入 ctx.report。"""
    ctx.report = generate_candidates(ctx.records, ctx.config)
    return ctx
