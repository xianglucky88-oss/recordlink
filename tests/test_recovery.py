"""中断恢复：崩溃后重连，主实体、引用、裁决、解释保持一致。"""
import pytest

from src.recordlink import pipeline, store
from src.recordlink.keyer import entity_id_of
from tests.conftest import rec, run, write_fixture


def _fixture(tmp_path):
    return write_fixture(tmp_path, [
        rec("a1", "alpha", "Acme", value=1, address="1 Main St"),
        rec("b1", "beta", "Acme", value=1, address="1 Main St"),
        rec("x1", "delta", "Other", value=9, refs=["alpha:a1"]),
    ])


def test_crash_mid_run_rolls_back(monkeypatch, tmp_path, con):
    fx = _fixture(tmp_path)
    from src.recordlink import merge

    def boom(ctx):
        raise RuntimeError("模拟崩溃")
    monkeypatch.setattr(merge, "run_stage", boom)
    with pytest.raises(RuntimeError):
        pipeline.run(con=con, fixture=fx)
    # 崩溃后：摄取与实体写入全部回滚
    assert store.count(con) == 0
    assert store.all_entities(con) == []
    assert store.current_generation(con) == 0
    # 重跑正常完成，状态一致
    monkeypatch.undo()
    r = run(con, fx)
    assert r["rows"] == 3 and r["generation"] == 1
    assert store.get_entity(con, entity_id_of(["alpha:a1", "beta:b1"]))["state"] == "active"


def test_orphan_unpublished_generation_cleaned(tmp_path, db_path):
    fx = _fixture(tmp_path)
    con = store.connect(db_path)
    run(con, fx)
    e_ab = entity_id_of(["alpha:a1", "beta:b1"])
    gen = store.current_generation(con)
    # 模拟崩溃残留：未发布的代次行（越过当前指针）
    con.execute("INSERT INTO generations(id,rule_version,status,note,created_at) "
                "VALUES(?,?,?,?,?)", (gen + 1, "1.0.0", "building", "crash", "t"))
    con.execute("INSERT INTO entities(entity_id,version,state,data,fingerprint,generation) "
                "VALUES('ent_ghost',1,'active','{}','fp',?)", (gen + 1,))
    con.execute("INSERT INTO explanations(subject_id,kind,generation,data) "
                "VALUES('ent_ghost','merge',?,'{}')", (gen + 1,))
    con.commit()
    con.close()

    con2 = store.connect(db_path)   # 重连触发 recover
    try:
        assert store.current_generation(con2) == gen
        assert store.get_entity(con2, "ent_ghost") is None
        assert store.explanations_of(con2, "ent_ghost") == {}
        # 正常数据完好：主实体、谱系、引用、解释一致
        ent = store.get_entity(con2, e_ab)
        assert ent["state"] == "active"
        assert store.lineage_of(con2, e_ab)
        assert len(store.active_migrations(con2)) == 1
        assert store.explanations_of(con2, e_ab)
    finally:
        con2.close()


def test_state_consistent_across_reconnect(tmp_path, db_path):
    fx = _fixture(tmp_path)
    con = store.connect(db_path)
    r1 = run(con, fx)
    con.close()
    con2 = store.connect(db_path)
    try:
        r2 = run(con2, fx)          # 重连后重跑：幂等，不增行不增代
        assert r2["rows"] == r1["rows"]
        assert r2["generation"] == r1["generation"]
    finally:
        con2.close()
