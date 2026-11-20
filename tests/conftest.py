import json

import pytest

from src.recordlink import pipeline, store
from src.recordlink.config import load as load_config


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def con(db_path):
    c = store.connect(db_path)
    yield c
    c.close()


def write_fixture(tmp_path, records, name="fixture.json"):
    p = tmp_path / name
    p.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return p


def run(con, fixture_path, config=None, **cfg_overrides):
    cfg = config or (load_config(cfg_overrides) if cfg_overrides else None)
    return pipeline.run(con=con, fixture=fixture_path, config=cfg)


# 常用记录构造
def rec(rid, src, name, value=0, status="active", ts="2026-01-01T00:00:00Z", **kw):
    r = {"id": rid, "src": src, "name": name, "value": value,
         "status": status, "ts": ts}
    r.update(kw)
    return r
