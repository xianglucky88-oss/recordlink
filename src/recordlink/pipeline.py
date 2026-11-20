"""薄编排：loader → normalizer → keyer → store（摄取幂等）
→ blocking → scoring → match → arbitration → merge → explain → 代次发布。

编排层不含业务判断；rollback 是对已应用操作的外部操作，不是流水线阶段。
"""
from datetime import datetime, timezone

from . import arbitration, blocking, explain, loader, match, merge, normalizer, scoring
from . import store as _store
from .config import load as load_config
from .keyer import key_of
from .models import Context, Record

STAGES = (blocking, scoring, match, arbitration, merge, explain)


def run(con=None, fixture=None, config=None, now=None):
    own = con is None
    con = con or _store.connect()
    cfg = config or load_config()
    now = now or datetime.now(timezone.utc)
    try:
        fed = 0
        for raw in loader.load(fixture):
            rec = normalizer.process(raw)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1

        records = _store.active_records(con)
        ctx = Context(con=con, config=cfg, now=now, records=records,
                      record_by_key={r["_key"]: r for r in records},
                      generation=_store.next_generation_id(con))
        for stage in STAGES:
            stage.run_stage(ctx)

        if ctx.changed:
            # 完整代次发布：全部变更与代次指针在同一事务提交。
            _store.publish_generation(con, ctx.generation, cfg.rule_version, now=now)
        con.commit()
        return {
            "fed": fed,
            "rows": _store.count(con),
            "entities": len(_store.all_entities(con, active_only=True)),
            "generation": _store.current_generation(con),
            "candidates": len(ctx.report.pairs),
            "estimated_comparisons": ctx.report.estimated_comparisons,
            "coverage": ctx.report.coverage,
            "review": len(_store.open_pendings(con)),
        }
    except Exception:
        con.rollback()
        raise
    finally:
        if own:
            con.close()
