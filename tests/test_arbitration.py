"""裁决：可信度/新鲜度/人工覆盖/字段类型；缺失、空值、删除、撤回；证据不足待审。"""
from datetime import datetime, timezone

from src.recordlink.arbitration import arbitrate_cluster
from src.recordlink.config import load as load_config

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _rec(key, src, ts="2026-01-01T00:00:00Z", **kw):
    r = {"_key": key, "src": src, "ts": ts, "_missing": []}
    r.update(kw)
    return r


def test_trust_decides_when_freshness_equal():
    cfg = load_config()
    members = [_rec("alpha:1", "alpha", name="From Alpha"),
               _rec("beta:1", "beta", name="From Beta")]
    out = arbitrate_cluster("ent_x", members, [], cfg, NOW)
    assert out["name"].state == "chosen"
    assert out["name"].value == "From Alpha"        # alpha 可信度 0.9 > beta 0.8


def test_freshness_decides_when_trust_equal():
    cfg = load_config()
    members = [_rec("alpha:1", "alpha", ts="2026-01-01T00:00:00Z", name="Old"),
               _rec("alpha:2", "alpha", ts="2026-05-01T00:00:00Z", name="New")]
    out = arbitrate_cluster("ent_x", members, [], cfg, NOW)
    assert out["name"].value == "New"


def test_manual_override_wins():
    cfg = load_config()
    members = [_rec("alpha:1", "alpha", name="A"), _rec("beta:1", "beta", name="B")]
    overrides = [{"scope": "entity", "target": "ent_x", "attr": "name",
                  "value": "Manual", "author": "ops", "ts": "2026-05-01"}]
    out = arbitrate_cluster("ent_x", members, overrides, cfg, NOW)
    assert out["name"].value == "Manual"
    assert "manual_override" in out["name"].reason

    overrides = [{"scope": "record", "target": "beta:1", "attr": "name",
                  "value": "RecordLevel", "author": "ops", "ts": "2026-05-01"}]
    out = arbitrate_cluster("ent_x", members, overrides, cfg, NOW)
    assert out["name"].value == "RecordLevel"


def test_missing_empty_deleted_retracted_are_distinct():
    cfg = load_config()
    # 缺失：不产生任何投票
    members = [_rec("alpha:1", "alpha", name="N", _missing=["status"]),
               _rec("beta:1", "beta", name="N", status="active")]
    out = arbitrate_cluster("ent_x", members, [], cfg, NOW)
    assert out["status"].value == "active"
    votes = [s for c in out["status"].candidates for s in c.sources]
    assert len(votes) == 1                          # 缺失方没有投票

    # 空值：作为“空”候选投票
    members = [_rec("alpha:1", "alpha", name="N", status=""),
               _rec("beta:1", "beta", name="N", status="")]
    out = arbitrate_cluster("ent_x", members, [], cfg, NOW)
    assert out["status"].state == "empty"

    # 删除：源删除标记作为“删除”候选，权重最高 => 主值移除该属性
    members = [_rec("alpha:1", "alpha", name="N", status={"$deleted": True}),
               _rec("beta:1", "beta", name="N", status={"$deleted": True}),
               _rec("gamma:1", "gamma", name="N", status="active")]
    out = arbitrate_cluster("ent_x", members, [], cfg, NOW)
    assert out["status"].state == "deleted"

    # 撤回：来源撤回的记录整体不计票
    members = [_rec("alpha:1", "alpha", name="N", status="active"),
               _rec("beta:1", "beta", name="N", status="sold", retracted=True)]
    out = arbitrate_cluster("ent_x", members, [], cfg, NOW)
    assert out["status"].value == "active"
    assert "撤回" in out["status"].reason


def test_insufficient_evidence_goes_pending():
    cfg = load_config()
    members = [_rec("s1:1", "src1", name="Value A"),
               _rec("s2:1", "src2", name="Value B")]   # 同源可信度、同时间戳
    out = arbitrate_cluster("ent_x", members, [], cfg, NOW)
    assert out["name"].state == "pending"           # 证据不足，保留待审
    assert out["name"].value is None
    assert len(out["name"].candidates) == 2


def test_field_type_latest_and_set():
    cfg = load_config({"field_types": {"status": "latest", "tags": "set"}})
    members = [
        _rec("alpha:1", "alpha", ts="2026-01-01T00:00:00Z",
             status="active", tags=["x", "y"]),
        _rec("beta:1", "beta", ts="2026-05-01T00:00:00Z",
             status="sold", tags=["y", "z"]),
    ]
    out = arbitrate_cluster("ent_x", members, [], cfg, NOW)
    assert out["status"].value == "sold"            # latest：最新证据胜出
    assert sorted(out["tags"].value) == ["x", "y", "z"]  # set：并集
