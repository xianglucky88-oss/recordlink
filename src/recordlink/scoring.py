"""单一职责：成对评分。

核心原则：区分“未知”与“强冲突”。
- 任一侧缺失/空值/被删除 => 该字段为 unknown，不计入分子也不计入分母；
  所有字段都未知时总分为 None（未知），绝不当作 0 分（不相似）处理。
- 冲突字段（如地址）双方都有值且相似度低于冲突阈值 => 强冲突，
  无论总分多高都阻断自动合并。
"""
import re

from .models import FieldScore, ScoreResult

MATCH_TOL = 0.8  # 字段相似度达到该值视为 match，否则为 conflict（仅用于标注）


def _norm(v):
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _sim_text(a, b):
    ta, tb = _norm(a), _norm(b)
    if ta == tb:
        return 1.0
    sa, sb = set(ta.split()), set(tb.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _sim_exact(a, b):
    return 1.0 if _norm(a) == _norm(b) else 0.0


def _sim_numeric(a, b):
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return _sim_exact(a, b)
    if fa == fb:
        return 1.0
    denom = max(abs(fa), abs(fb), 1e-9)
    return max(0.0, 1.0 - abs(fa - fb) / denom)


_COMPARATORS = {
    "name": _sim_text,
    "address": _sim_text,
    "status": _sim_exact,
    "value": _sim_numeric,
}


def _field_state(rec, field):
    """判断字段在记录中的取值状态：missing / empty / deleted / value。"""
    if field in rec.get("_missing", []) or field not in rec:
        return "missing"
    v = rec.get(field)
    if isinstance(v, dict) and v.get("$deleted"):
        return "deleted"
    if v is None or v == "":
        return "empty"
    return "value"


def score_pair(ra, rb, config):
    """对两条记录评分，返回 ScoreResult。score 为 None 表示无共同已知证据。"""
    fields, unknown = [], []
    acc = wsum = 0.0
    strong = False
    for f, w in sorted(config.field_weights.items()):
        sa, sb = _field_state(ra, f), _field_state(rb, f)
        if sa != "value" or sb != "value":
            # 缺失/空值/删除 => 未知证据，不参与加权
            fields.append(FieldScore(f, "unknown", None))
            unknown.append(f)
            continue
        sim = _COMPARATORS.get(f, _sim_text)(ra[f], rb[f])
        state = "match" if sim >= MATCH_TOL else "conflict"
        if f in config.conflict_fields and sim < config.conflict_threshold:
            strong = True
            state = "conflict"
        fields.append(FieldScore(f, state, sim))
        acc += w * sim
        wsum += w
    score = (acc / wsum) if wsum else None
    return ScoreResult(a=ra["_key"], b=rb["_key"], score=score, fields=fields,
                       strong_conflict=strong, unknown_fields=unknown)


def run_stage(ctx):
    """流水线阶段：对候选对逐对评分。"""
    by_key = ctx.record_by_key
    ctx.scored = [score_pair(by_key[a], by_key[b], ctx.config)
                  for a, b in ctx.report.pairs]
    return ctx
