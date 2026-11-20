"""主键：源限定，同源自去重，跨源不覆盖。"""
from src.recordlink import store
from src.recordlink.keyer import key_of, split_key
from tests.conftest import rec, run, write_fixture


def test_same_id_different_sources_do_not_overwrite(tmp_path, con):
    fx = write_fixture(tmp_path, [
        rec("r-1", "alpha", "Alpha One", value=10),
        rec("r-1", "beta", "Beta One", value=20),
        rec("r-1", "alpha", "Alpha One", value=10),  # 同源同 id 重复 => 去重
    ])
    result = run(con, fx)
    assert result["fed"] == 3
    assert result["rows"] == 2  # 跨源同 id 两条都保留，同源重复被去重
    keys = sorted(k for k, _ in store.all_records(con))
    assert keys == ["alpha:r-1", "beta:r-1"]
    data = {k: d for k, d in store.all_records(con)}
    assert data["alpha:r-1"]["name"] == "Alpha One"
    assert data["beta:r-1"]["name"] == "Beta One"


def test_key_scoped_by_source():
    assert key_of({"id": "x", "src": "Alpha"}) == "alpha:x"
    assert key_of({"id": "x", "src": "beta"}) == "beta:x"
    assert key_of({"id": "x", "src": ""}) == "x"      # 无源输入保持兼容
    assert split_key("alpha:x") == ("alpha", "x")


def test_key_hash_fallback_without_id():
    k1 = key_of({"src": "a", "name": "n"})
    k2 = key_of({"src": "a", "name": "n"})
    k3 = key_of({"src": "b", "name": "n"})
    assert k1 == k2 and k1 != k3
