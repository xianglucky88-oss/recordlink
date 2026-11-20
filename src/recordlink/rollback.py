"""单一职责：撤销（rollback）。

撤销一个合并操作时区分两种情形：

1. 无后续依赖：直接按 op.payload.prev_versions 恢复快照——主实体回到
   合并前状态，被吸收的实体恢复 active，引用迁移标记撤回。
2. 有后续依赖（后续合并已消费该实体）：做补偿而不是快照回滚——
   只把该操作贡献的成员从当前实体中剥离，剩余成员原地重算裁决，
   被剥离成员形成拆分实体；后续无关内容（后到的成员、人工覆盖、
   不相关属性）保持不变。引用迁移按来源记录重新指向。

撤销本身也是幂等操作：重复撤销返回首次结果，不产生重复补偿。
"""
import hashlib
from datetime import datetime, timezone

from . import arbitration as _arb
from . import merge as _merge
from . import store
from .config import load as load_config
from .keyer import entity_id_of


def _load_members(con, member_keys):
    recs = []
    for k in member_keys:
        data = store.get_record(con, k)
        if data is not None:
            data = dict(data)
            data["_key"] = k
            recs.append(data)
    return recs


def _dependents(con, op):
    """后续依赖：代次更晚、成员集合与该操作相交、仍处 applied 的合并操作。"""
    return [o for o in store.ops_involving(con, op["payload"].get("members", []),
                                           since_generation=op["generation"] + 1)
            if o["op_id"] != op["op_id"]]


def undo(con, op_id, config=None, now=None):
    """撤销指定合并操作；返回撤销结果。重复调用幂等。"""
    config = config or load_config()
    now = now or datetime.now(timezone.utc)
    op = store.get_op(con, op_id)
    if op is None:
        raise KeyError(f"未知操作: {op_id}")
    if op["kind"] != "merge":
        raise ValueError(f"只能撤销 merge 操作，实际为 {op['kind']}")
    if len(op["payload"].get("members", [])) < 2:
        raise ValueError("单记录实体的物化操作无可撤销的合并")
    if op["status"] in ("undone", "compensated"):
        return {"op_id": op_id, "status": f"already_{op['status']}"}

    gid = store.next_generation_id(con)
    deps = _dependents(con, op)
    if not deps:
        result = _undo_restore(con, op, gid, now, config)
    else:
        result = _undo_compensate(con, op, deps, config, gid, now)
    store.publish_generation(con, gid, config.rule_version,
                             note=f"undo {op_id}", now=now)
    con.commit()
    return result


def _undo_restore(con, op, gid, now, config):
    """无依赖撤销：恢复快照，撤回该操作引入的引用迁移。

    恢复后，若成员记录不再属于任何活跃实体，为其补建单记录实体，
    保证“每条活跃记录都有主实体”这一不变量。
    """
    payload = op["payload"]
    restored = []
    for eid, prev_ver in sorted(payload["prev_versions"].items()):
        cur = store.get_entity(con, eid)
        if cur is None:
            continue
        if eid == payload["entity_id"]:
            # 主实体：标记 undone（保留数据供审计，不再参与活跃处理）
            store.put_entity(con, eid, cur["version"] + 1, "undone",
                             cur["data"], cur["fingerprint"], gid)
            restored.append({"entity_id": eid, "to": "undone"})
        else:
            snap = store.entity_version(con, eid, prev_ver)
            if snap:
                store.put_entity(con, eid, cur["version"] + 1, snap["state"],
                                 snap["data"], snap["fingerprint"], gid)
                restored.append({"entity_id": eid, "to": snap["state"],
                                 "restored_version": prev_ver})
    reverted = []
    for m in store.migrations_to(con, payload["entity_id"]):
        if not m["undone"]:
            store.mark_migration_undone(con, m["migration_id"])
            reverted.append(m["migration_id"])
    singletons = _ensure_singletons(con, payload["members"], config, gid, now)
    store.set_op_status(con, op["op_id"], "undone")
    undo_op_id = _merge.op_id_of("undo", op["op_id"], "restore")
    store.put_op(con, undo_op_id, "undo",
                 {"undo_of": op["op_id"], "mode": "restore",
                  "restored": restored, "reverted_migrations": reverted,
                  "singletons": singletons},
                 gid, now=now)
    store.put_explanation(con, payload["entity_id"], "undo", gid, {
        "undo_of": op["op_id"], "mode": "restore", "restored": restored,
        "reverted_migrations": reverted, "singletons": singletons,
        "reason": "无后续依赖，按版本快照恢复，引用迁移已撤回"})
    return {"op_id": op["op_id"], "status": "undone", "mode": "restore",
            "restored": restored, "reverted_migrations": reverted,
            "singletons": singletons, "undo_op_id": undo_op_id}


def _ensure_singletons(con, member_keys, config, gid, now):
    """为没有被任何活跃实体覆盖的成员记录补建单记录实体。"""
    covered = set()
    for e in store.all_entities(con, active_only=True):
        covered.update(e["data"].get("members", []))
    overrides = store.all_overrides(con)
    created = []
    for key in sorted(set(member_keys) - covered):
        recs = _load_members(con, [key])
        if not recs or recs[0].get("deleted") or recs[0].get("retracted"):
            continue
        eid = entity_id_of([key])
        decisions = _arb.arbitrate_cluster(eid, recs, overrides, config, now)
        data = {"entity_id": eid, "members": [key],
                "attrs": {a: d.value for a, d in decisions.items()
                          if d.state == "chosen"},
                "pending_attrs": sorted(a for a, d in decisions.items()
                                        if d.state == "pending"),
                "deleted_attrs": sorted(a for a, d in decisions.items()
                                        if d.state == "deleted"),
                "empty_attrs": sorted(a for a, d in decisions.items()
                                      if d.state == "empty"),
                "cluster_id": f"singleton_after_undo",
                "rule_version": config.rule_version}
        fp = _arb.fingerprint_of(recs, overrides, config, eid)
        old = store.get_entity(con, eid)
        store.put_entity(con, eid, (old["version"] + 1) if old else 1,
                         "active", data, fp, gid)
        store.replace_lineage(con, eid, _merge.lineage_rows(decisions), gid)
        created.append(eid)
    return created


def _undo_compensate(con, op, deps, config, gid, now):
    """有依赖撤销：补偿式拆分，只移除该操作的贡献，保留后续无关内容。"""
    payload = op["payload"]
    target_members = set(payload["members"])
    # 当前包含这些成员的活跃实体（后续合并的产物）
    current = None
    for e in store.all_entities(con, active_only=True):
        members = set(e["data"].get("members", []))
        if members & target_members:
            if current is None or len(members & target_members) > \
                    len(set(current["data"]["members"]) & target_members):
                current = e
    if current is None:
        store.set_op_status(con, op["op_id"], "compensated")
        return {"op_id": op["op_id"], "status": "compensated",
                "mode": "noop", "reason": "成员已不在任何活跃实体中"}

    cur_members = set(current["data"]["members"])
    removed = sorted(cur_members & target_members)
    remaining = sorted(cur_members - target_members)
    overrides = store.all_overrides(con)
    split_entity = None

    if not remaining:
        # 当前实体完全由该操作贡献 => 整体退役
        store.put_entity(con, current["entity_id"], current["version"] + 1,
                         "undone", current["data"], current["fingerprint"], gid)
    else:
        # 剩余成员原地重算：后续合并带入的成员与人工覆盖全部保留
        recs = _load_members(con, remaining)
        decisions = _arb.arbitrate_cluster(current["entity_id"], recs,
                                           overrides, config, now)
        data = dict(current["data"])
        data["members"] = remaining
        data["attrs"] = {a: d.value for a, d in decisions.items()
                         if d.state == "chosen"}
        data["pending_attrs"] = sorted(a for a, d in decisions.items()
                                       if d.state == "pending")
        fp = _arb.fingerprint_of(recs, overrides, config, current["entity_id"])
        store.put_entity(con, current["entity_id"], current["version"] + 1,
                         "active", data, fp, gid)
        store.replace_lineage(con, current["entity_id"],
                              _merge.lineage_rows(decisions), gid)

    if removed:
        # 被剥离成员形成拆分实体（若历史上有同成员实体则复活之）
        split_id = entity_id_of(removed)
        recs = _load_members(con, removed)
        decisions = _arb.arbitrate_cluster(split_id, recs, overrides, config, now)
        data = {"entity_id": split_id, "members": removed,
                "attrs": {a: d.value for a, d in decisions.items()
                          if d.state == "chosen"},
                "pending_attrs": sorted(a for a, d in decisions.items()
                                        if d.state == "pending"),
                "deleted_attrs": sorted(a for a, d in decisions.items()
                                        if d.state == "deleted"),
                "empty_attrs": sorted(a for a, d in decisions.items()
                                      if d.state == "empty"),
                "cluster_id": f"split_of_{op['op_id']}",
                "rule_version": config.rule_version}
        fp = _arb.fingerprint_of(recs, overrides, config, split_id)
        old = store.get_entity(con, split_id)
        store.put_entity(con, split_id, (old["version"] + 1) if old else 1,
                         "active", data, fp, gid)
        store.replace_lineage(con, split_id, _merge.lineage_rows(decisions), gid)
        split_entity = split_id
        # 引用迁移：来源记录被剥离的引用改指拆分实体
        for m in store.active_migrations(con):
            if m["from_ref"] in set(removed) and m["to_ref"] == current["entity_id"]:
                store.mark_migration_undone(con, m["migration_id"])
                mid = hashlib.sha256(
                    f"{m['from_ref']}|{split_id}".encode()).hexdigest()[:16]
                store.add_migration(con, mid, m["from_ref"], split_id,
                                    op["op_id"], gid, referrer=m["referrer"])

    store.set_op_status(con, op["op_id"], "compensated")
    comp_op_id = _merge.op_id_of("compensate", op["op_id"], str(gid))
    comp_payload = {"undo_of": op["op_id"], "mode": "compensate",
                    "dependents": [d["op_id"] for d in deps],
                    "removed_members": removed, "remaining_members": remaining,
                    "split_entity": split_entity,
                    "boundary": "仅移除该操作贡献的成员；后续合并的成员、"
                                "人工覆盖与无关属性保持不变"}
    store.put_op(con, comp_op_id, "compensate", comp_payload, gid, now=now)
    for eid in {current["entity_id"], split_entity} - {None}:
        store.put_explanation(con, eid, "compensate", gid, comp_payload)
    return {"op_id": op["op_id"], "status": "compensated", "mode": "compensate",
            "removed_members": removed, "remaining_members": remaining,
            "split_entity": split_entity, "compensate_op_id": comp_op_id}
