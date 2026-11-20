"""单一职责：派生稳定主键。

主键是“源限定”的：``src:id``。同一来源内相同 id 幂等去重；
不同来源的相同 id 是不同记录，绝不互相覆盖。
"""
import hashlib
import json


def key_of(record):
    rid = record.get("id")
    src = str(record.get("src") or "").strip().lower()
    if rid:
        rid = str(rid).strip()
        return f"{src}:{rid}" if src else rid
    # 无 id：以规范化内容派生稳定散列键（含 src，避免跨源碰撞）。
    payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def split_key(key):
    """拆出 (src, id)；无源前缀时 src 为空串。"""
    if ":" in key:
        src, rid = key.split(":", 1)
        return src, rid
    return "", key


def entity_id_of(members):
    """主实体 id：由成员键集合确定性派生（同成员 => 同实体）。"""
    digest = hashlib.sha256(",".join(sorted(members)).encode()).hexdigest()[:16]
    return f"ent_{digest}"
