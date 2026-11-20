"""端到端：候选→匹配→裁决→合并→解释→代次，含迟到证据、规则换版、撤回。"""
from src.recordlink import explain, store
from src.recordlink.keyer import entity_id_of
from tests.conftest import rec, run, write_fixture

ACME = ["alpha:a1", "beta:b1"]


def _v1():
    return [
        rec("a1", "alpha", "Acme Corp", value=100, address="1 Main St",
            ts="2026-01-01T00:00:00Z"),
        rec("b1", "beta", "Acme Corp", value=100, address="1 Main St",
            ts="2026-01-02T00:00:00Z"),
        rec("c1", "gamma", "Acme Corp", value=100, address="9 Other Rd",
            ts="2026-01-03T00:00:00Z"),
        rec("a1", "delta", "Delta One", value=5, ts="2026-01-04T00:00:00Z"),
        rec("e1", "alpha", "Referrer", value=1, refs=["beta:b1"],
            ts="2026-01-05T00:00:00Z"),
    ]


def test_full_flow_merge_conflict_refs(tmp_path, con):
    fx = write_fixture(tmp_path, _v1())
    result = run(con, fx)
    assert result["fed"] == 5 and result["rows"] == 5
    assert result["entities"] == 4            # acme 合并 + 3 个单记录实体
    assert result["review"] == 2              # 两对地址强冲突进入待审

    e_acme = entity_id_of(ACME)
    ent = store.get_entity(con, e_acme)
    assert ent["state"] == "active"
    assert ent["data"]["attrs"]["name"] == "Acme Corp"
    assert ent["data"]["attrs"]["address"] == "1 Main St"

    # 谱系：两个来源的记录都在案，且都被选中
    lin = store.lineage_of(con, e_acme)
    name_lin = [r for r in lin if r["attr"] == "name"]
    assert {r["record_key"] for r in name_lin} == set(ACME)
    assert all(r["selected"] for r in name_lin)

    # 引用迁移：e1 对 beta:b1 的引用迁移到主实体
    mig = store.active_migrations(con)
    assert len(mig) == 1 and mig[0]["from_ref"] == "beta:b1"
    assert mig[0]["to_ref"] == e_acme

    # 解释对应实际决定：合并边、分数、阈值、规则版本齐全
    expl = explain.explain_entity(con, e_acme)
    merge_expl = expl["decisions"]["merge"]["data"]
    assert merge_expl["edges"][0]["score"] == 1.0
    assert merge_expl["rule_version"] == "1.0.0"
    arb_expl = expl["decisions"]["arbitration"]["data"]
    assert arb_expl["attrs"]["name"]["state"] == "chosen"

    # 跨源同 id：alpha:a1 与 delta:a1 是两个不同实体
    e_delta = entity_id_of(["delta:a1"])
    assert store.get_entity(con, e_delta)["state"] == "active"
    assert e_delta != e_acme


def test_rerun_is_idempotent(tmp_path, con):
    fx = write_fixture(tmp_path, _v1())
    r1 = run(con, fx)
    e_acme = entity_id_of(ACME)
    v1 = store.get_entity(con, e_acme)["version"]
    r2 = run(con, fx)
    assert r2["rows"] == r1["rows"] == 5
    assert r2["generation"] == r1["generation"]      # 无变化不发布新代次
    assert store.get_entity(con, e_acme)["version"] == v1


def test_late_evidence_recomputes_only_affected(tmp_path, con):
    fx1 = write_fixture(tmp_path, _v1(), "v1.json")
    run(con, fx1)
    e_delta = entity_id_of(["delta:a1"])
    delta_v = store.get_entity(con, e_delta)["version"]
    delta_expl_gen = explain.explain_entity(con, e_delta)[
        "decisions"]["merge"]["generation"]

    late = _v1() + [rec("c9", "gamma", "Acme Corp", value=100,
                        address="1 Main St", ts="2026-02-01T00:00:00Z")]
    fx2 = write_fixture(tmp_path, late, "v2.json")
    r2 = run(con, fx2)
    assert r2["generation"] == 2

    # 受影响的：acme 实体扩展为三人，旧实体被取代
    e_acme2 = entity_id_of(ACME + ["gamma:c9"])
    assert store.get_entity(con, e_acme2)["state"] == "active"
    assert store.get_entity(con, entity_id_of(ACME))["state"] == "superseded"
    # 不受影响的：delta 实体版本与解释原样沿用
    assert store.get_entity(con, e_delta)["version"] == delta_v
    assert explain.explain_entity(con, e_delta)[
        "decisions"]["merge"]["generation"] == delta_expl_gen


def test_rule_version_bump_recomputes(tmp_path, con):
    fx = write_fixture(tmp_path, _v1())
    run(con, fx)
    e_delta = entity_id_of(["delta:a1"])
    v1 = store.get_entity(con, e_delta)["version"]
    r = run(con, fx, rule_version="2.0.0")
    assert r["generation"] == 2
    assert store.get_entity(con, e_delta)["version"] == v1 + 1
    expl = explain.explain_entity(con, e_delta)
    assert expl["decisions"]["merge"]["data"]["rule_version"] == "2.0.0"


def test_retraction_withdraws_contribution(tmp_path, con):
    fx1 = write_fixture(tmp_path, _v1(), "v1.json")
    run(con, fx1)
    e_acme = entity_id_of(ACME)
    assert store.get_entity(con, e_acme)["state"] == "active"

    retracted = _v1()
    retracted[1]["retracted"] = True                # beta 撤回 b1
    fx2 = write_fixture(tmp_path, retracted, "v2.json")
    r = run(con, fx2)
    assert r["rows"] == 5                           # 墓碑保留，行数不变
    assert store.get_entity(con, e_acme)["state"] == "superseded"
    e_a1 = entity_id_of(["alpha:a1"])
    solo = store.get_entity(con, e_a1)
    assert solo["state"] == "active"
    assert solo["data"]["members"] == ["alpha:a1"]  # 主实体不再含被撤回成员

    # 撤回再恢复：原合并按相同证据可重放回来
    run(con, fx1)
    assert store.get_entity(con, e_acme)["state"] == "active"


def test_deleted_record_excluded_from_processing(tmp_path, con):
    records = _v1()
    records[4]["deleted"] = True                    # e1 被源删除
    fx = write_fixture(tmp_path, records)
    r = run(con, fx)
    assert r["rows"] == 5                           # 墓碑仍在
    assert r["entities"] == 3                       # e1 不产生实体
    assert store.active_migrations(con) == []       # 引用也不再生效
