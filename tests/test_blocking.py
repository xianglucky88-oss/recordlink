"""候选生成：超大桶与缺字段时的覆盖范围与计算成本说明。"""
from src.recordlink.blocking import generate_candidates
from src.recordlink.config import load as load_config


def _rec(key, **kw):
    r = {"_key": key, "_missing": []}
    r.update(kw)
    return r


def test_oversized_bucket_skipped_and_reported():
    cfg = load_config({"max_bucket_size": 3, "blocking_keys": ("status",)})
    records = [_rec(f"k{i}", name=f"n{i}", status="active") for i in range(5)]
    report = generate_candidates(records, cfg)
    assert report.pairs == []                      # 桶过大被跳过
    assert report.estimated_comparisons == 0
    assert report.skipped_comparisons == 5 * 4 // 2  # 跳过的比较数有明确账目
    assert report.oversized and report.oversized[0]["size"] == 5
    assert any("过大" in n for n in report.notes)   # 报告说明成本与漏合并风险
    assert report.coverage == 0.0


def test_missing_fields_reported_in_coverage():
    cfg = load_config({"blocking_keys": ("name_prefix", "status")})
    records = [
        _rec("a", name="Acme", status="active"),
        _rec("b", name="Acme", status="active"),
        _rec("c", status="active"),                 # 缺 name
        _rec("d", name="Solo", _missing=[]),        # 缺 status，name 桶内无邻居
    ]
    records[2]["_missing"] = ["name"]
    records[3]["_missing"] = ["status"]
    report = generate_candidates(records, cfg)
    assert ("a", "b") in report.pairs
    assert "c" in report.missing_fields["name_prefix"]  # 缺字段记录被点名
    assert "d" in report.missing_fields["status"]
    # c 仍被 status 规则覆盖（候选对包含它），d 两条规则都未产生候选
    assert "c" in report.covered
    assert "d" in report.uncovered
    assert report.coverage == 0.75
    assert any("缺字段" in n for n in report.notes)


def test_cost_scales_with_bucket_size():
    cfg = load_config({"blocking_keys": ("status",)})
    records = [_rec(f"k{i}", name=f"n{i}", status="x") for i in range(4)]
    report = generate_candidates(records, cfg)
    assert report.estimated_comparisons == 6        # C(4,2)
    assert len(report.pairs) == 6
