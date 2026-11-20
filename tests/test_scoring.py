"""评分：区分未知（缺证据）与强冲突（证据矛盾）。"""
from src.recordlink.config import load as load_config
from src.recordlink.scoring import score_pair


def _rec(key, **kw):
    r = {"_key": key, "_missing": []}
    r.update(kw)
    return r


def test_unknown_is_not_zero_score():
    cfg = load_config()
    a = _rec("a", name="Acme", value=1, status="active")
    b = _rec("b", value=1, status="active", _missing=["name"])  # name 缺失
    s = score_pair(a, b, cfg)
    assert "name" in s.unknown_fields
    assert s.score is not None and s.score > 0.9   # 未知字段不计入分母

    c = _rec("c", _missing=["name", "value", "status"])
    s2 = score_pair(a, c, cfg)
    assert s2.score is None                         # 无共同已知证据 => 未知


def test_strong_conflict_distinct_from_unknown():
    cfg = load_config()
    a = _rec("a", name="Acme", address="1 Main St", value=1, status="active")
    b = _rec("b", name="Acme", address="9 Other Rd", value=1, status="active")
    s = score_pair(a, b, cfg)
    assert s.strong_conflict is True                # 地址双方有值且矛盾
    addr = next(f for f in s.fields if f.field == "address")
    assert addr.state == "conflict"

    b2 = _rec("b2", name="Acme", value=1, status="active", _missing=["address"])
    s2 = score_pair(a, b2, cfg)
    assert s2.strong_conflict is False              # 一侧缺失是未知，不是冲突
    assert "address" in s2.unknown_fields


def test_deleted_field_counts_as_unknown():
    cfg = load_config()
    a = _rec("a", name="Acme", address="1 Main St", value=1, status="active")
    b = _rec("b", name="Acme", address={"$deleted": True}, value=1, status="active")
    s = score_pair(a, b, cfg)
    assert "address" in s.unknown_fields
    assert s.strong_conflict is False


def test_empty_value_is_unknown_not_conflict():
    cfg = load_config()
    a = _rec("a", name="Acme", address="1 Main St", value=1, status="active")
    b = _rec("b", name="Acme", address="", value=1, status="active")
    s = score_pair(a, b, cfg)
    assert "address" in s.unknown_fields
    assert s.strong_conflict is False
