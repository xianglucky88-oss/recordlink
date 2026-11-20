"""撤销：无依赖恢复快照；连续合并后撤销前一步 => 补偿边界明确、不动后续无关内容。"""
from src.recordlink import explain, rollback, store
from src.recordlink.keyer import entity_id_of
from tests.conftest import rec, run, write_fixture

AB = ["alpha:a1", "beta:b1"]
ABC = ["alpha:a1", "beta:b1", "gamma:c1"]


def _pair(tmp_path, name="f.json"):
    return write_fixture(tmp_path, [
        rec("a1", "alpha", "Acme", value=1, address="1 Main St"),
        rec("b1", "beta", "Acme", value=1, address="1 Main St"),
        rec("x1", "delta", "Other", value=9, refs=["alpha:a1"]),
    ], name)


def _merge_op(con, members):
    ops = [o for o in store.ops_involving(con, members)
           if sorted(o["payload"]["members"]) == sorted(members)]
    assert ops, f"未找到成员为 {members} 的合并操作"
    return ops[0]


def test_undo_without_dependents_restores(tmp_path, con):
    fx = _pair(tmp_path)
    run(con, fx)
    e_ab = entity_id_of(AB)
    op = _merge_op(con, AB)

    result = rollback.undo(con, op["op_id"])
    assert result["status"] == "undone" and result["mode"] == "restore"

    # 主实体退役；成员记录恢复为各自的单记录实体
    assert store.get_entity(con, e_ab)["state"] == "undone"
    for key in AB:
        solo = store.get_entity(con, entity_id_of([key]))
        assert solo["state"] == "active"
        assert solo["data"]["members"] == [key]
    # 引用迁移被撤回
    assert store.active_migrations(con) == []
    # 撤销操作本身幂等
    again = rollback.undo(con, op["op_id"])
    assert again["status"] == "already_undone"
    # 解释记录了撤销决定
    expl = explain.explain_entity(con, e_ab)
    assert expl["decisions"]["undo"]["data"]["undo_of"] == op["op_id"]


def test_undo_earlier_merge_after_later_merge_compensates(tmp_path, con):
    fx1 = _pair(tmp_path, "v1.json")
    run(con, fx1)
    op1 = _merge_op(con, AB)

    # 第二次运行：gamma:c1 到达并并入 => 连续合并
    fx2 = write_fixture(tmp_path, [
        rec("a1", "alpha", "Acme", value=1, address="1 Main St"),
        rec("b1", "beta", "Acme", value=1, address="1 Main St"),
        rec("c1", "gamma", "Acme", value=1, address="1 Main St"),
        rec("x1", "delta", "Other", value=9, refs=["alpha:a1"]),
    ], "v2.json")
    run(con, fx2)
    e_abc = entity_id_of(ABC)
    assert store.get_entity(con, e_abc)["state"] == "active"
    e_x = entity_id_of(["delta:x1"])
    x_version = store.get_entity(con, e_x)["version"]

    # 撤销第一步（op1）：后续合并已消费其产物 => 补偿而非快照回滚
    result = rollback.undo(con, op1["op_id"])
    assert result["status"] == "compensated"
    assert result["removed_members"] == AB
    assert result["remaining_members"] == ["gamma:c1"]

    # 补偿边界：op1 的贡献被剥离；后续合并带来的 c1 留在原实体
    cur = store.get_entity(con, e_abc)
    assert cur["state"] == "active"
    assert cur["data"]["members"] == ["gamma:c1"]
    assert cur["data"]["attrs"]["name"] == "Acme"   # c1 的属性仍在
    # 被剥离成员形成拆分实体（复活原 AB 实体 id）
    split = store.get_entity(con, entity_id_of(AB))
    assert split["state"] == "active"
    assert split["data"]["members"] == AB
    # 后续无关内容不受影响：x1 实体版本未被撤销操作推进
    assert store.get_entity(con, e_x)["version"] == x_version
    # 引用迁移改指拆分实体（alpha:a1 现在属于拆分实体）
    migs = store.active_migrations(con)
    assert len(migs) == 1 and migs[0]["to_ref"] == entity_id_of(AB)
    # 补偿解释落在受影响实体上
    expl = explain.explain_entity(con, e_abc)
    assert expl["decisions"]["compensate"]["data"]["mode"] == "compensate"


def test_undo_unknown_op_raises(con):
    import pytest
    with pytest.raises(KeyError):
        rollback.undo(con, "op_nonexistent")


def test_undo_singleton_op_rejected(tmp_path, con):
    import pytest
    fx = _pair(tmp_path)
    run(con, fx)
    solo_ops = [o for o in store.ops_involving(con, ["delta:x1"])
                if o["payload"]["members"] == ["delta:x1"]]
    assert solo_ops
    with pytest.raises(ValueError):
        rollback.undo(con, solo_ops[0]["op_id"])
