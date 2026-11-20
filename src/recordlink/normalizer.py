"""单一职责：字段规整/规范化（对记录做单一职责变换：normalizer）。规范阶段。

- 保持原有合法输入行为：src 小写、name 去空白、value 转 int。
- 记录原本缺失的字段（``_missing``），让下游能区分“缺失”与“空值”。
- 记录级标记：``deleted``（源删除墓碑）、``retracted``（来源撤回）。
- 字段级删除标记 ``{"$deleted": true}`` 原样保留，由裁决阶段解释。
"""


def process(record):
    rec = dict(record)
    missing = [f for f in ("name", "value") if f not in rec]
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = _norm_text_field(rec.get("name", ""))
    rec["value"] = _to_int(rec.get("value", 0))
    if "address" in rec and rec["address"] is not None:
        rec["address"] = _norm_text_field(rec["address"])
    if "refs" in rec:
        refs = rec["refs"]
        rec["refs"] = [str(r).strip() for r in refs] if isinstance(refs, (list, tuple)) else [str(refs).strip()]
    rec["deleted"] = bool(rec.get("deleted", False))
    rec["retracted"] = bool(rec.get("retracted", False))
    rec["_missing"] = missing
    return rec


def _norm_text_field(v):
    # 字段级删除标记 {"$deleted": true} 原样保留，由裁决阶段解释
    if isinstance(v, dict) and v.get("$deleted"):
        return v
    return " ".join(str(v).split())


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v  # 非数值原样保留，由评分阶段按文本处理
