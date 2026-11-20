"""单一职责：主数据合并。

- 每个合并操作有确定性 op_id：重复提交是幂等空操作（重复操作可识别）；
- 实体带单调版本号，应用操作时校验期望版本，不一致则报并发冲突；
- 合并保留：属性（裁决结果）、来源谱系（lineage）、引用迁移记录；
- 被吸收的旧实体标记 superseded 并记入 op.payload.prev_versions，供撤销恢复。
"""
import hashlib
import json

from . import store


class VersionConflict(Exception):
    """并发版本冲突：期望版本与当前版本不一致。"""


def op_id_of(kind, entity_id, fingerprint):
    digest = hashlib.sha256(f"{kind}|{entity_id}|{fingerprint}".encode()).hexdigest()[:16]
    return f"op_{digest}"


def build_merge_payload(plan, arbitration, prev_versions, supersedes, config):
    attrs = {a: d.value for a, d in arbitration.items() if d.state == "chosen"}
    return {
        "entity_id": plan.entity_id,
        "members": plan.members,
        "fingerprint": plan.fingerprint,
        "prev_versions": prev_versions,      # {entity_id: version} 应用前的版本快照指针
        "supersedes": supersedes,            # 被吸收的实体 id
        "entity_data": {
            "entity_id": plan.entity_id,
            "members": plan.members,
            "attrs": attrs,
            "pending_attrs": sorted(a for a, d in arbitration.items()
                                    if d.state == "pending"),
            "deleted_attrs": sorted(a for a, d in arbitration.items()
                                    if d.state == "deleted"),
            "empty_attrs": sorted(a for a, d in arbitration.items()
                                  if d.state == "empty"),
            "cluster_id": plan.cluster_id,
            "rule_version": config.rule_version,
        },
    }


def apply_merge_op(con, payload, generation, now=None):
    """应用一个合并操作。

    幂等：同一 op 已应用且实体状态与指纹一致 => 返回 'duplicate' 空操作；
    实体曾被取代/撤销/撤回 => 同一 op 可重放（状态恢复一致）。
    并发安全：prev_versions 与当前版本不一致 => 抛 VersionConflict。
    """
    op_id = op_id_of("merge", payload["entity_id"], payload["fingerprint"])
    ent = store.get_entity(con, payload["entity_id"])
    op = store.get_op(con, op_id)
    if op and op["status"] == "applied" and ent and ent["state"] == "active" \
            and ent["fingerprint"] == payload["fingerprint"]:
        return {"op_id": op_id, "status": "duplicate"}
    for eid, expected in payload["prev_versions"].items():
        cur = store.get_entity(con, eid)
        cur_version = cur["version"] if cur else 0
        if cur_version != expected:
            raise VersionConflict(
                f"实体 {eid} 版本冲突：期望 {expected}，当前 {cur_version}")
    entity_id = payload["entity_id"]
    prev = payload["prev_versions"].get(entity_id, 0)
    store.put_entity(con, entity_id, prev + 1, "active",
                     payload["entity_data"], payload["fingerprint"], generation)
    for eid in payload["supersedes"]:
        cur = store.get_entity(con, eid)
        if cur and cur["state"] == "active":
            store.put_entity(con, eid, cur["version"] + 1, "superseded",
                             cur["data"], cur["fingerprint"], generation)
    store.put_op(con, op_id, "merge", payload, generation, now=now)
    store.touch_op(con, op_id, generation)
    return {"op_id": op_id, "status": "applied"}


def lineage_rows(arbitration):
    """从裁决结果生成谱系行：每个属性的每个投票来源及其是否被选中。"""
    rows = []
    for attr, dec in arbitration.items():
        chosen_value = dec.value if dec.state == "chosen" else None
        for cand in dec.candidates:
            for src in cand.sources:
                if not src:
                    continue
                selected = (dec.state == "chosen" and cand.state == "value"
                            and cand.value == chosen_value)
                rows.append({"attr": attr, "record_key": src["key"],
                             "value": cand.value if cand.state == "value" else None,
                             "state": cand.state, "selected": selected,
                             "rule_version": dec.rule_version})
    return rows


def _desired_migrations(con):
    """期望的引用迁移：活跃记录的 refs 指向其他实体的成员键。"""
    member_to_entity = {}
    for e in store.all_entities(con, active_only=True):
        for m in e["data"].get("members", []):
            member_to_entity[m] = e["entity_id"]
    desired = {}
    for rec in store.active_records(con):
        referrer = member_to_entity.get(rec["_key"])
        for ref in rec.get("refs", []):
            target = member_to_entity.get(ref)
            if target and target != referrer:
                mid = hashlib.sha256(
                    f"{rec['_key']}|{ref}|{target}".encode()).hexdigest()[:16]
                desired[mid] = {"migration_id": mid, "from_ref": ref,
                                "to_ref": target, "referrer": referrer or ""}
    return desired


def reconcile_refs(con, generation, now=None):
    """对齐引用迁移记录：新增缺失的迁移，失效的标记 undone。返回是否有变化。"""
    desired = _desired_migrations(con)
    active = {m["migration_id"]: m for m in store.active_migrations(con)}
    changed = False
    op_id = None
    for mid, m in desired.items():
        if mid not in active:
            if op_id is None:
                op_id = op_id_of("ref_sync", "generation", str(generation))
            store.add_migration(con, mid, m["from_ref"], m["to_ref"], op_id,
                                generation, referrer=m["referrer"])
            changed = True
    for mid, m in active.items():
        if mid not in desired:
            store.mark_migration_undone(con, mid)
            changed = True
    if op_id and not store.has_op(con, op_id):
        store.put_op(con, op_id, "ref_sync",
                     {"generation": generation,
                      "added": sorted(desired)}, generation, now=now)
    return changed


def entity_refs(con, entity_id):
    """实体当前对外部实体的引用（由迁移记录派生，不冗余存储，避免过期）。"""
    return [{"from_record": m["from_ref"], "to_entity": m["to_ref"]}
            for m in store.active_migrations(con)
            if m["referrer"] == entity_id]


def run_stage(ctx):
    """流水线阶段：对非沿用的簇应用合并操作，并 reconcile 引用迁移。"""
    planned = {p.entity_id: p for p in ctx.plans}
    all_known = {e["entity_id"]: e for e in store.all_entities(ctx.con)}
    active_entities = {eid: e for eid, e in all_known.items()
                       if e["state"] == "active"}
    cluster_members = set()
    for p in ctx.plans:
        cluster_members.update(p.members)

    for plan in ctx.plans:
        if plan.carried:
            continue
        arbitration = ctx.arbitrations[plan.entity_id]
        prev_versions, supersedes = {}, []
        # 当前活跃实体中，成员与本簇相交但 id 不同的，将被吸收（superseded）。
        for eid, ent in active_entities.items():
            if eid == plan.entity_id:
                continue
            if set(ent["data"].get("members", [])) & set(plan.members):
                prev_versions[eid] = ent["version"]
                supersedes.append(eid)
        prev = all_known.get(plan.entity_id)
        prev_versions[plan.entity_id] = prev["version"] if prev else 0
        payload = build_merge_payload(plan, arbitration, prev_versions,
                                      sorted(supersedes), ctx.config)
        result = apply_merge_op(ctx.con, payload, ctx.generation, now=ctx.now)
        store.replace_lineage(ctx.con, plan.entity_id,
                              lineage_rows(arbitration), ctx.generation)
        ctx.ops.append(result)
        ctx.changed = True

    # 成员已全部失效（删除/撤回）的实体 => 标记 retracted（保留审计痕迹）。
    for eid, ent in active_entities.items():
        if eid in planned:
            continue
        members = set(ent["data"].get("members", []))
        if members and not (members & cluster_members):
            store.put_entity(ctx.con, eid, ent["version"] + 1, "retracted",
                             ent["data"], ent["fingerprint"], ctx.generation)
            ctx.changed = True

    if reconcile_refs(ctx.con, ctx.generation, now=ctx.now):
        ctx.changed = True
    return ctx
