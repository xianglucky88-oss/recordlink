"""单一职责：属性级主值裁决。

主值选择综合：来源可信度、新鲜度（指数衰减）、人工覆盖、字段类型策略。
严格区分四种“无值”状态：
- missing  字段缺失：不作为任何投票；
- empty    显式空值：作为“空”候选投票；
- deleted  源删除标记 ``{"$deleted": true}``：作为“删除”候选投票；
- retracted 整条记录被来源撤回：其所有投票一并撤回。
证据不足（前两名权重接近）时不强行选值，保留待审。
"""
import hashlib
import json
import math
from datetime import datetime, timezone

from . import store
from .keyer import entity_id_of
from .models import ATTR_META_FIELDS, AttrCandidate, AttrDecision, ClusterPlan

DELETE_MARKER = "$deleted"


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def freshness_weight(ts, now, half_life_days):
    """新鲜度权重：指数衰减。缺时间戳按 0.5 折减。"""
    t = _parse_ts(ts)
    if t is None:
        return 0.5
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - t).total_seconds() / 86400.0)
    return 0.5 ** (age_days / max(half_life_days, 1e-9))


def trust_of(src, config):
    return float(config.source_trust.get(src, config.default_trust))


def _record_vote(rec, attr, config, now):
    """返回 (state, value, weight, source_desc)；state: missing/value/empty/deleted。"""
    if attr in rec.get("_missing", []) or attr not in rec:
        return ("missing", None, 0.0, None)
    v = rec[attr]
    trust = trust_of(rec.get("src", ""), config)
    w = trust * freshness_weight(rec.get("ts"), now, config.freshness_half_life_days)
    src_desc = {"key": rec["_key"], "src": rec.get("src", ""), "ts": rec.get("ts"),
                "trust": trust}
    if isinstance(v, dict) and v.get(DELETE_MARKER):
        return ("deleted", None, w, src_desc)
    if v is None or v == "":
        return ("empty", None, w, src_desc)
    return ("value", v, w, src_desc)


def fingerprint_of(member_records, overrides, config, entity_id):
    """簇指纹：规则版本 + 成员内容 + 相关人工覆盖 + 裁决相关配置。

    指纹不变 => 历史裁决与解释原样沿用（只重算受影响部分）。
    """
    relevant_cfg = {
        "rule_version": config.rule_version,
        "source_trust": config.source_trust,
        "default_trust": config.default_trust,
        "field_types": config.field_types,
        "freshness_half_life_days": config.freshness_half_life_days,
        "pending_margin": config.pending_margin,
    }
    keys = {r["_key"] for r in member_records}
    rel_overrides = [
        (o["scope"], o["target"], o["attr"], o["value"]) for o in overrides
        if (o["scope"] == "record" and o["target"] in keys)
        or (o["scope"] == "entity" and o["target"] == entity_id)
    ]
    payload = {
        "cfg": relevant_cfg,
        "members": sorted((r["_key"], _content_hash(r)) for r in member_records),
        "overrides": sorted(rel_overrides, key=repr),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _content_hash(rec):
    clean = {k: v for k, v in rec.items()}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()[:16]


def arbitrate_cluster(entity_id, member_records, overrides, config, now):
    """对一个簇的全部业务字段做主值裁决。返回 {attr: AttrDecision}。"""
    active = [r for r in member_records if not r.get("retracted")]
    retracted = [r["_key"] for r in member_records if r.get("retracted")]
    attrs = sorted({a for r in active for a in r} - ATTR_META_FIELDS)
    ov_entity = {o["attr"]: o for o in overrides
                 if o["scope"] == "entity" and o["target"] == entity_id}
    ov_record = {}
    for o in overrides:
        if o["scope"] == "record" and o["target"] in {r["_key"] for r in active}:
            ov_record.setdefault(o["attr"], []).append(o)

    out = {}
    for attr in attrs:
        ftype = config.field_types.get(attr, "scalar")
        # 人工覆盖优先（实体级 > 记录级），覆盖本身也作为解释依据记录。
        if attr in ov_entity:
            o = ov_entity[attr]
            out[attr] = AttrDecision(attr, "chosen", o["value"], [],
                                     f"manual_override(entity:{o['author']})",
                                     config.rule_version)
            continue
        if attr in ov_record:
            o = sorted(ov_record[attr], key=lambda x: x["ts"])[-1]
            out[attr] = AttrDecision(attr, "chosen", o["value"], [],
                                     f"manual_override(record:{o['target']}:{o['author']})",
                                     config.rule_version)
            continue

        votes = []
        for rec in active:
            state, value, w, src_desc = _record_vote(rec, attr, config, now)
            if state == "missing":
                continue
            votes.append((state, value, w, src_desc))
        if not votes:
            continue  # 全部缺失：主实体不出现该字段

        if ftype == "set":
            out[attr] = _decide_set(attr, votes, config, retracted)
            continue
        if ftype == "latest":
            out[attr] = _decide_latest(attr, votes, active, config, retracted)
            continue
        out[attr] = _decide_scalar(attr, votes, config, retracted)
    return out


def _group_candidates(votes):
    """把投票聚合为候选：value 按值聚合；empty/deleted 各自成候选。"""
    groups = {}
    for state, value, w, src in votes:
        gkey = (state, json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                if state == "value" else "")
        g = groups.setdefault(gkey, {"state": state, "value": value, "weight": 0.0,
                                     "sources": []})
        g["weight"] += w
        g["sources"].append(src)
    cands = [AttrCandidate(g["value"], g["state"], round(g["weight"], 6), g["sources"])
             for g in groups.values()]
    cands.sort(key=lambda c: -c.weight)
    return cands


def _decide_scalar(attr, votes, config, retracted):
    cands = _group_candidates(votes)
    top = cands[0]
    reason = "trust+freshness 加权"
    if retracted:
        reason += f"；已撤回来源不计入: {retracted}"
    if len(cands) > 1 and top.weight > 0 and cands[1].weight > 0 and \
            cands[1].weight / top.weight > config.pending_margin:
        return AttrDecision(attr, "pending", None, cands,
                            f"证据不足：前两名权重接近"
                            f"（{top.weight:.3f} vs {cands[1].weight:.3f}），保留待审",
                            config.rule_version)
    if top.state == "deleted":
        return AttrDecision(attr, "deleted", None, cands,
                            f"删除候选权重最高，主值移除该属性；{reason}",
                            config.rule_version)
    if top.state == "empty":
        return AttrDecision(attr, "empty", None, cands,
                            f"空值候选权重最高，主值置空；{reason}", config.rule_version)
    return AttrDecision(attr, "chosen", top.value, cands, reason, config.rule_version)


def _decide_latest(attr, votes, active, config, retracted):
    def ts_of(vote):
        src = vote[3] or {}
        t = _parse_ts(src.get("ts"))
        return t or datetime.min.replace(tzinfo=timezone.utc)
    best = max(votes, key=ts_of)
    ties = [v for v in votes if ts_of(v) == ts_of(best)
            and v[0] == "value" and v[1] != best[1]]
    cands = _group_candidates(votes)
    if ties:
        return AttrDecision(attr, "pending", None, cands,
                            "最新时间戳并列且取值不同，保留待审", config.rule_version)
    state, value = best[0], best[1]
    if state == "deleted":
        return AttrDecision(attr, "deleted", None, cands, "最新证据为源删除",
                            config.rule_version)
    if state == "empty":
        return AttrDecision(attr, "empty", None, cands, "最新证据为空值",
                            config.rule_version)
    return AttrDecision(attr, "chosen", value, cands, "latest 策略取最新",
                        config.rule_version)


def _decide_set(attr, votes, config, retracted):
    values = []
    for state, value, _w, _src in votes:
        if state != "value":
            continue
        items = value if isinstance(value, list) else [value]
        for it in items:
            if it not in values:
                values.append(it)
    cands = _group_candidates(votes)
    if not values:
        return AttrDecision(attr, "empty", None, cands, "集合无有效元素",
                            config.rule_version)
    return AttrDecision(attr, "chosen", values, cands, "set 策略取并集",
                        config.rule_version)


def run_stage(ctx):
    """流水线阶段：指纹未变的簇沿用历史裁决（carried），只重算受影响的簇。"""
    overrides = store.all_overrides(ctx.con)
    for cluster in ctx.clusters:
        members = [ctx.record_by_key[k] for k in cluster.members]
        entity_id = entity_id_of(cluster.members)
        fp = fingerprint_of(members, overrides, ctx.config, entity_id)
        existing = store.get_entity(ctx.con, entity_id)
        carried = (existing is not None and existing["state"] == "active"
                   and existing["fingerprint"] == fp)
        ctx.plans.append(ClusterPlan(cluster.cluster_id, entity_id,
                                     sorted(cluster.members), fp, carried))
        if carried:
            continue
        ctx.arbitrations[entity_id] = arbitrate_cluster(
            entity_id, members, overrides, ctx.config, ctx.now)
        # 裁决层待审：证据不足的属性（先收集，发布代次时统一落库）
        for attr, dec in ctx.arbitrations[entity_id].items():
            if dec.state == "pending":
                ctx.pending_items.append({
                    "item_id": f"rev_attr_{entity_id}_{attr}", "kind": "attr",
                    "data": {"entity_id": entity_id, "attr": attr,
                             "reason": dec.reason,
                             "candidates": [
                                 {"value": c.value, "state": c.state,
                                  "weight": c.weight, "sources": c.sources}
                                 for c in dec.candidates]}})
        ctx.changed = True
    return ctx
