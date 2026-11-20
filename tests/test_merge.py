"""合并：并发版本冲突识别、重复操作幂等、谱系与引用迁移记录。"""
import pytest

from src.recordlink import store
from src.recordlink.keyer import entity_id_of
from src.recordlink.merge import (VersionConflict, apply_merge_op,
                                  build_merge_payload, op_id_of)
from tests.conftest import rec, run, write_fixture


def _two_record_fixture(tmp_path):
    return write_fixture(tmp_path, [
        rec("a1", "alpha", "Acme", value=1, address="1 Main St"),
        rec("b1", "beta", "Acme", value=1, address="1 Main St"),
    ])


def _merge_op(con, members):
    ops = store.ops_involving(con, members)
    assert ops, "应存在合并操作"
    return ops[0]


def test_duplicate_op_is_noop(tmp_path, con):
    fx = _two_record_fixture(tmp_path)
    run(con, fx)
    eid = entity_id_of(["alpha:a1", "beta:b1"])
    v1 = store.get_entity(con, eid)["version"]
    op = _merge_op(con, ["alpha:a1"])
    again = apply_merge_op(con, op["payload"], generation=99)
    assert again["status"] == "duplicate"
    assert store.get_entity(con, eid)["version"] == v1   # 版本不被重复推进


def test_version_conflict_detected(tmp_path, con):
    fx = _two_record_fixture(tmp_path)
    run(con, fx)
    eid = entity_id_of(["alpha:a1", "beta:b1"])
    op = _merge_op(con, ["alpha:a1"])
    payload = dict(op["payload"])
    payload["prev_versions"] = {eid: 999}                # 过期的期望版本
    payload["fingerprint"] = payload["fingerprint"] + "_x"
    with pytest.raises(VersionConflict):
        apply_merge_op(con, payload, generation=99)


def test_merge_keeps_lineage_and_ref_migration(tmp_path, con):
    fx = write_fixture(tmp_path, [
        rec("a1", "alpha", "Acme", value=1, address="1 Main St"),
        rec("b1", "beta", "Acme", value=1, address="1 Main St"),
        rec("r1", "gamma", "Ref", value=2, refs=["alpha:a1", "beta:b1"]),
    ])
    run(con, fx)
    eid = entity_id_of(["alpha:a1", "beta:b1"])
    # 属性谱系：每个属性的每个来源投票都在案
    lin = store.lineage_of(con, eid)
    assert {r["record_key"] for r in lin} == {"alpha:a1", "beta:b1"}
    assert {r["attr"] for r in lin} >= {"name", "value", "address"}
    # 引用迁移：两条引用都迁移到主实体，发起方是 r1 所在实体
    migs = store.active_migrations(con)
    assert {m["from_ref"] for m in migs} == {"alpha:a1", "beta:b1"}
    assert all(m["to_ref"] == eid for m in migs)
    assert all(m["referrer"] == entity_id_of(["gamma:r1"]) for m in migs)


def test_op_id_deterministic():
    a = op_id_of("merge", "ent_x", "fp1")
    b = op_id_of("merge", "ent_x", "fp1")
    c = op_id_of("merge", "ent_x", "fp2")
    assert a == b and a != c
