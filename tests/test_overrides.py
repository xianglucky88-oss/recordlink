"""人工覆盖与证据不足待审：贯穿流水线的行为。"""
from src.recordlink import store
from src.recordlink.keyer import entity_id_of
from tests.conftest import rec, run, write_fixture

AB = ["alpha:a1", "beta:b1"]


def _fx(tmp_path, name_a="Acme A", name_b="Acme A"):
    return write_fixture(tmp_path, [
        rec("a1", "alpha", name_a, value=1, address="1 Main St"),
        rec("b1", "beta", name_b, value=1, address="1 Main St"),
    ])


def test_entity_override_applies_and_explained(tmp_path, con):
    fx = _fx(tmp_path)
    run(con, fx)
    e_ab = entity_id_of(AB)
    store.set_override(con, "entity", e_ab, "name", "Acme Ltd",
                       author="ops", now=None)
    con.commit()
    r = run(con, fx)
    assert r["generation"] == 2                     # 覆盖变化触发受影响重算
    ent = store.get_entity(con, e_ab)
    assert ent["data"]["attrs"]["name"] == "Acme Ltd"
    expl = store.explanations_of(con, e_ab)["arbitration"]["data"]
    assert "manual_override" in expl["attrs"]["name"]["reason"]
    # 移除覆盖后按证据重新裁决
    store.remove_override(con, "entity", e_ab, "name")
    con.commit()
    run(con, fx)
    assert store.get_entity(con, e_ab)["data"]["attrs"]["name"] == "Acme A"


def test_record_override_applies(tmp_path, con):
    fx = _fx(tmp_path)
    run(con, fx)
    e_ab = entity_id_of(AB)
    store.set_override(con, "record", "beta:b1", "status", "closed", author="qa")
    con.commit()
    run(con, fx)
    assert store.get_entity(con, e_ab)["data"]["attrs"]["status"] == "closed"


def test_insufficient_evidence_pending_review(tmp_path, con):
    # 两个默认可信度来源的 status 冲突：合并分数足够，但该属性证据打平
    fx = write_fixture(tmp_path, [
        rec("a1", "src1", "Same Name", value=1, address="1 Main St",
            status="active"),
        rec("b1", "src2", "Same Name", value=1, address="1 Main St",
            status="sold"),
    ])
    r = run(con, fx)
    assert r["review"] >= 1
    e = entity_id_of(["src1:a1", "src2:b1"])
    ent = store.get_entity(con, e)
    assert "status" in ent["data"]["pending_attrs"]
    assert "status" not in ent["data"]["attrs"]      # 待审属性不进主值
    assert ent["data"]["attrs"]["name"] == "Same Name"
    pendings = store.open_pendings(con, kind="attr")
    assert pendings and pendings[0]["data"]["attr"] == "status"
    # 迟到证据打破僵局后，待审自动解除
    fx2 = write_fixture(tmp_path, [
        rec("a1", "src1", "Same Name", value=1, address="1 Main St",
            status="active"),
        rec("b1", "src2", "Same Name", value=1, address="1 Main St",
            status="sold"),
        rec("c1", "alpha", "Same Name", value=1, address="1 Main St",
            status="active"),
    ], "v2.json")
    run(con, fx2)
    e3 = entity_id_of(["src1:a1", "src2:b1", "alpha:c1"])
    assert store.get_entity(con, e3)["data"]["attrs"]["status"] == "active"
    assert store.open_pendings(con, kind="attr") == []
