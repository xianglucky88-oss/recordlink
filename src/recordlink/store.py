"""单一职责：本地 sqlite3 持久化。

表结构：
- records        摄取记录（源限定主键，幂等 upsert；含 deleted/retracted 墓碑）
- entities       主实体当前状态（版本号用于并发冲突检测）
- entity_versions 主实体历史版本（撤销/审计用）
- lineage        属性级来源谱系（哪个记录的哪个值被选中/未选中）
- ref_migrations 引用迁移记录（合并后引用从成员键迁移到主实体）
- ops            操作日志（幂等、撤销、恢复的依据）
- explanations   解释（按代次保留历史，对应实际决定与证据）
- pending_review 证据不足保留待审项
- overrides      人工覆盖（跨代次生效）
- generations    代次（完整发布：全部变更与指针更新在同一事务）
- meta           当前代次指针

中断恢复：所有代次写入与 current_generation 指针更新在同一事务提交，
崩溃只会留下未提交的事务（sqlite 自动回滚）。recover() 作为第二道防线，
清理任何“代次 > 当前指针”的孤儿行，保证恢复后主实体、引用、裁决、解释一致。
"""
import json
import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS records(
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities(
    entity_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,            -- active | superseded | retracted | undone
    data TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_versions(
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    data TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL,
    PRIMARY KEY(entity_id, version)
);
CREATE TABLE IF NOT EXISTS lineage(
    entity_id TEXT NOT NULL,
    attr TEXT NOT NULL,
    record_key TEXT NOT NULL,
    value TEXT,
    state TEXT NOT NULL,            -- value | empty | deleted
    selected INTEGER NOT NULL,      -- 1 => 该记录的值被选中为主值
    rule_version TEXT NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY(entity_id, attr, record_key)
);
CREATE TABLE IF NOT EXISTS ref_migrations(
    migration_id TEXT PRIMARY KEY,
    from_ref TEXT NOT NULL,         -- 原引用（成员记录键）
    to_ref TEXT NOT NULL,           -- 迁移目标（主实体 id）
    referrer TEXT NOT NULL DEFAULT '',  -- 发起引用的实体 id
    op_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ops(
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- merge | ref_sync | undo | compensate
    payload TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'applied',  -- applied | undone | compensated
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS explanations(
    subject_id TEXT NOT NULL,       -- entity_id
    kind TEXT NOT NULL,             -- merge | arbitration | undo | compensate
    generation INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY(subject_id, kind, generation)
);
CREATE TABLE IF NOT EXISTS pending_review(
    item_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- pair | attr
    data TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'   -- open | resolved
);
CREATE TABLE IF NOT EXISTS overrides(
    scope TEXT NOT NULL,            -- record | entity
    target TEXT NOT NULL,           -- record_key 或 entity_id
    attr TEXT NOT NULL,
    value TEXT NOT NULL,
    author TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY(scope, target, attr)
);
CREATE TABLE IF NOT EXISTS generations(
    id INTEGER PRIMARY KEY,
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL,           -- published（单事务发布，不存在中间态）
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path=None):
    con = sqlite3.connect(str(path or DB_PATH))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    recover(con)
    return con


def commit(con):
    con.commit()


# ---------- 恢复 ----------

def recover(con):
    """清理未发布代次的孤儿行；返回恢复报告。幂等，connect 时自动执行。"""
    cur = current_generation(con)
    stray = [r[0] for r in con.execute(
        "SELECT id FROM generations WHERE id > ? OR status != 'published'", (cur,))]
    report = {"current_generation": cur, "dropped_generations": stray, "cleaned": {}}
    if not stray:
        return report
    for table in ("entities", "entity_versions", "lineage", "ref_migrations",
                  "ops", "explanations", "pending_review"):
        n = con.execute(f"DELETE FROM {table} WHERE generation > ?", (cur,)).rowcount
        if n:
            report["cleaned"][table] = n
    con.execute("DELETE FROM generations WHERE id > ? OR status != 'published'", (cur,))
    con.commit()
    return report


# ---------- meta / 代次 ----------

def _meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def current_generation(con):
    return int(_meta_get(con, "current_generation", 0))


def next_generation_id(con):
    row = con.execute("SELECT COALESCE(MAX(id),0) FROM generations").fetchone()
    return int(row[0]) + 1


def publish_generation(con, gid, rule_version, note="", now=None):
    """在同一事务内登记代次并推进当前指针（调用方负责整体 commit）。"""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    con.execute("INSERT INTO generations(id,rule_version,status,note,created_at) "
                "VALUES(?,?,?,?,?)", (gid, rule_version, "published", note, ts))
    _meta_set(con, "current_generation", gid)


# ---------- records ----------

def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False)))


def get_record(con, key):
    row = con.execute("SELECT data FROM records WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def count(con):
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(r["key"], json.loads(r["data"]))
            for r in con.execute("SELECT key,data FROM records")]


def active_records(con):
    """活跃记录：排除 deleted（源删除）与 retracted（来源撤回）。"""
    out = []
    for key, data in all_records(con):
        if data.get("deleted") or data.get("retracted"):
            continue
        data = dict(data)
        data["_key"] = key
        out.append(data)
    return out


# ---------- entities ----------

def _row_to_entity(row):
    return {"entity_id": row["entity_id"], "version": row["version"],
            "state": row["state"], "data": json.loads(row["data"]),
            "fingerprint": row["fingerprint"], "generation": row["generation"]}


def get_entity(con, entity_id):
    row = con.execute("SELECT * FROM entities WHERE entity_id=?",
                      (entity_id,)).fetchone()
    return _row_to_entity(row) if row else None


def all_entities(con, active_only=False):
    sql = "SELECT * FROM entities"
    if active_only:
        sql += " WHERE state='active'"
    return [_row_to_entity(r) for r in con.execute(sql)]


def put_entity(con, entity_id, version, state, data, fingerprint, generation):
    """写入实体当前状态并记录历史版本（版本号单调递增，用于并发冲突检测）。"""
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    con.execute("INSERT INTO entities(entity_id,version,state,data,fingerprint,generation) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET version=excluded.version, "
                "state=excluded.state, data=excluded.data, fingerprint=excluded.fingerprint, "
                "generation=excluded.generation",
                (entity_id, version, state, payload, fingerprint, generation))
    con.execute("INSERT OR REPLACE INTO entity_versions"
                "(entity_id,version,state,data,fingerprint,generation) "
                "VALUES(?,?,?,?,?,?)", (entity_id, version, state, payload,
                                        fingerprint, generation))


def entity_version(con, entity_id, version):
    row = con.execute("SELECT * FROM entity_versions WHERE entity_id=? AND version=?",
                      (entity_id, version)).fetchone()
    if not row:
        return None
    return {"entity_id": row["entity_id"], "version": row["version"],
            "state": row["state"], "data": json.loads(row["data"]),
            "fingerprint": row["fingerprint"], "generation": row["generation"]}


# ---------- lineage ----------

def replace_lineage(con, entity_id, rows, generation):
    """整体替换某实体的谱系（重算受影响部分时按实体粒度替换）。"""
    con.execute("DELETE FROM lineage WHERE entity_id=?", (entity_id,))
    for r in rows:
        con.execute("INSERT OR REPLACE INTO lineage"
                    "(entity_id,attr,record_key,value,state,selected,rule_version,generation) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (entity_id, r["attr"], r["record_key"],
                     json.dumps(r.get("value"), ensure_ascii=False), r["state"],
                     1 if r.get("selected") else 0, r["rule_version"], generation))


def lineage_of(con, entity_id):
    return [{"attr": r["attr"], "record_key": r["record_key"],
             "value": json.loads(r["value"]) if r["value"] is not None else None,
             "state": r["state"], "selected": bool(r["selected"]),
             "rule_version": r["rule_version"], "generation": r["generation"]}
            for r in con.execute("SELECT * FROM lineage WHERE entity_id=? "
                                 "ORDER BY attr, record_key", (entity_id,))]


# ---------- ref_migrations ----------

def add_migration(con, migration_id, from_ref, to_ref, op_id, generation,
                  referrer=""):
    con.execute("INSERT OR IGNORE INTO ref_migrations"
                "(migration_id,from_ref,to_ref,referrer,op_id,generation,undone) "
                "VALUES(?,?,?,?,?,?,0)",
                (migration_id, from_ref, to_ref, referrer, op_id, generation))


def active_migrations(con):
    return [{"migration_id": r["migration_id"], "from_ref": r["from_ref"],
             "to_ref": r["to_ref"], "referrer": r["referrer"],
             "op_id": r["op_id"], "generation": r["generation"]}
            for r in con.execute("SELECT * FROM ref_migrations WHERE undone=0")]


def mark_migration_undone(con, migration_id):
    con.execute("UPDATE ref_migrations SET undone=1 WHERE migration_id=?", (migration_id,))


def migrations_to(con, to_ref):
    return [{"migration_id": r["migration_id"], "from_ref": r["from_ref"],
             "to_ref": r["to_ref"], "op_id": r["op_id"], "generation": r["generation"],
             "undone": bool(r["undone"])}
            for r in con.execute("SELECT * FROM ref_migrations WHERE to_ref=?", (to_ref,))]


# ---------- ops ----------

def has_op(con, op_id):
    return con.execute("SELECT 1 FROM ops WHERE op_id=?", (op_id,)).fetchone() is not None


def put_op(con, op_id, kind, payload, generation, now=None, status="applied"):
    ts = (now or datetime.now(timezone.utc)).isoformat()
    con.execute("INSERT OR IGNORE INTO ops(op_id,kind,payload,generation,status,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (op_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 generation, status, ts))


def get_op(con, op_id):
    row = con.execute("SELECT * FROM ops WHERE op_id=?", (op_id,)).fetchone()
    if not row:
        return None
    return {"op_id": row["op_id"], "kind": row["kind"],
            "payload": json.loads(row["payload"]), "generation": row["generation"],
            "status": row["status"], "created_at": row["created_at"]}


def set_op_status(con, op_id, status):
    con.execute("UPDATE ops SET status=? WHERE op_id=?", (status, op_id))


def touch_op(con, op_id, generation):
    """重放已有操作时刷新其代次并置回 applied（保持依赖时序有意义）。"""
    con.execute("UPDATE ops SET generation=?, status='applied' WHERE op_id=?",
                (generation, op_id))


def ops_involving(con, member_keys, since_generation=0):
    """找出成员集合与 member_keys 相交、代次 >= since 的已应用 merge 操作。"""
    out = []
    for r in con.execute("SELECT * FROM ops WHERE kind='merge' AND generation>=? "
                         "AND status='applied' ORDER BY generation, created_at",
                         (since_generation,)):
        payload = json.loads(r["payload"])
        if set(payload.get("members", [])) & set(member_keys):
            out.append({"op_id": r["op_id"], "kind": r["kind"], "payload": payload,
                        "generation": r["generation"], "status": r["status"],
                        "created_at": r["created_at"]})
    return out


# ---------- explanations ----------

def put_explanation(con, subject_id, kind, generation, data):
    con.execute("INSERT OR REPLACE INTO explanations(subject_id,kind,generation,data) "
                "VALUES(?,?,?,?)",
                (subject_id, kind, generation, json.dumps(data, ensure_ascii=False)))


def explanations_of(con, subject_id):
    """每个 kind 取最新代次的解释（历史代次仍保留在表中，可追溯）。"""
    out = {}
    for r in con.execute("SELECT * FROM explanations WHERE subject_id=? "
                         "ORDER BY generation", (subject_id,)):
        out[r["kind"]] = {"generation": r["generation"], "data": json.loads(r["data"])}
    return out


# ---------- pending_review ----------

def put_pending(con, item_id, kind, data, generation):
    con.execute("INSERT INTO pending_review(item_id,kind,data,generation,status) "
                "VALUES(?,?,?,?,'open') "
                "ON CONFLICT(item_id) DO UPDATE SET data=excluded.data, "
                "generation=excluded.generation, status='open'",
                (item_id, kind, json.dumps(data, ensure_ascii=False), generation))


def resolve_pending(con, item_id):
    con.execute("UPDATE pending_review SET status='resolved' WHERE item_id=?", (item_id,))


def open_pendings(con, kind=None):
    sql = "SELECT * FROM pending_review WHERE status='open'"
    args = ()
    if kind:
        sql += " AND kind=?"
        args = (kind,)
    return [{"item_id": r["item_id"], "kind": r["kind"], "data": json.loads(r["data"]),
             "generation": r["generation"]}
            for r in con.execute(sql, args)]


# ---------- overrides ----------

def set_override(con, scope, target, attr, value, author="manual", now=None):
    if scope not in ("record", "entity"):
        raise ValueError("scope 必须是 record 或 entity")
    ts = (now or datetime.now(timezone.utc)).isoformat()
    con.execute("INSERT INTO overrides(scope,target,attr,value,author,ts) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(scope,target,attr) DO UPDATE SET value=excluded.value, "
                "author=excluded.author, ts=excluded.ts",
                (scope, target, attr, json.dumps(value, ensure_ascii=False), author, ts))


def remove_override(con, scope, target, attr):
    con.execute("DELETE FROM overrides WHERE scope=? AND target=? AND attr=?",
                (scope, target, attr))


def all_overrides(con):
    return [{"scope": r["scope"], "target": r["target"], "attr": r["attr"],
             "value": json.loads(r["value"]), "author": r["author"], "ts": r["ts"]}
            for r in con.execute("SELECT * FROM overrides")]
