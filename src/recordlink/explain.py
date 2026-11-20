"""单一职责：可解释归并。

解释必须对应实际发生的决定与历史证据：
- merge 解释记录促成该实体的真实决策边（分数、冲突标记、决策理由）；
- arbitration 解释记录每个属性的真实候选、权重、来源与规则版本；
- 指纹未变的实体不重写解释（历史决定原样保留，按代次可追溯）。
"""
from . import merge as _merge
from . import store


def build_merge_explanation(plan, cluster, decisions, config):
    edges = []
    for d in cluster.edges:
        edges.append({"a": d.a, "b": d.b, "score": d.score, "reason": d.reason})
    return {
        "entity_id": plan.entity_id,
        "cluster_id": plan.cluster_id,
        "members": plan.members,
        "edges": edges,                      # 实际促成合并的决策边
        "singleton": len(plan.members) == 1,
        "evidence": [{"record_key": m} for m in plan.members],
        "rule_version": config.rule_version,
        "thresholds": {"auto": config.auto_merge_threshold,
                       "review": config.review_threshold},
    }


def build_arbitration_explanation(plan, arbitration):
    attrs = {}
    for attr, dec in sorted(arbitration.items()):
        attrs[attr] = {
            "state": dec.state,
            "value": dec.value,
            "reason": dec.reason,
            "rule_version": dec.rule_version,
            "candidates": [
                {"value": c.value, "state": c.state, "weight": c.weight,
                 "sources": c.sources} for c in dec.candidates
            ],
        }
    return {"entity_id": plan.entity_id, "attrs": attrs}


def run_stage(ctx):
    """流水线阶段：为本代次实际重算的实体写解释（沿用的不动）。"""
    clusters = {c.cluster_id: c for c in ctx.clusters}
    for plan in ctx.plans:
        if plan.carried:
            continue
        cluster = clusters[plan.cluster_id]
        store.put_explanation(
            ctx.con, plan.entity_id, "merge", ctx.generation,
            build_merge_explanation(plan, cluster, ctx.decisions, ctx.config))
        store.put_explanation(
            ctx.con, plan.entity_id, "arbitration", ctx.generation,
            build_arbitration_explanation(plan, ctx.arbitrations[plan.entity_id]))
    if ctx.changed:
        _sync_pendings(ctx)
    return ctx


def _sync_pendings(ctx):
    """发布代次时对齐待审表：当前待审项置 open，不再待审的置 resolved。"""
    current = {}
    for item in ctx.pending_items:
        current[item["item_id"]] = item
        store.put_pending(ctx.con, item["item_id"], item["kind"],
                          item["data"], ctx.generation)
    for p in store.open_pendings(ctx.con):
        if p["item_id"] not in current:
            store.resolve_pending(ctx.con, p["item_id"])


def explain_entity(con, entity_id):
    """汇总一个主实体的完整解释：当前状态、决定、谱系、引用、操作历史。"""
    ent = store.get_entity(con, entity_id)
    if not ent:
        return None
    expl = store.explanations_of(con, entity_id)
    member_ops = store.ops_involving(con, ent["data"].get("members", []))
    return {
        "entity_id": entity_id,
        "version": ent["version"],
        "state": ent["state"],
        "data": ent["data"],
        "decisions": expl,                       # 各 kind 最新代次的解释
        "lineage": store.lineage_of(con, entity_id),
        "refs": _merge.entity_refs(con, entity_id),
        "migrations_to_me": store.migrations_to(con, entity_id),
        "ops": [{"op_id": o["op_id"], "kind": o["kind"],
                 "generation": o["generation"], "status": o["status"]}
                for o in member_ops],
        "current_generation": store.current_generation(con),
    }
