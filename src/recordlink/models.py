"""共享数据结构：各阶段之间传递的不可变结果对象。"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Record:
    key: str
    data: dict[str, Any] = field(default_factory=dict)


# ---------- blocking ----------

@dataclass
class CandidateReport:
    """候选生成报告：说明覆盖范围与计算成本。"""
    pairs: list[tuple[str, str]] = field(default_factory=list)
    estimated_comparisons: int = 0        # 实际展开的成对比较数（成本）
    skipped_comparisons: int = 0          # 超大桶被跳过的比较数（成本控制）
    coverage: float = 0.0                 # 至少产生一个候选对的记录占比
    covered: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)   # 缺字段/唯一桶而未覆盖的记录
    oversized: list[dict] = field(default_factory=list)  # 被跳过的超大桶明细
    missing_fields: dict = field(default_factory=dict)   # 规则 -> 缺该字段的记录键
    notes: list[str] = field(default_factory=list)


# ---------- scoring ----------

@dataclass
class FieldScore:
    field: str
    state: str                # match | conflict | unknown
    similarity: float | None  # unknown 时为 None


@dataclass
class ScoreResult:
    a: str
    b: str
    score: float | None       # 没有任何共同已知字段时为 None（未知，不是 0）
    fields: list[FieldScore] = field(default_factory=list)
    strong_conflict: bool = False
    unknown_fields: list[str] = field(default_factory=list)


# ---------- match ----------

@dataclass
class Decision:
    a: str
    b: str
    kind: str                 # auto_merge | review | no_merge
    reason: str
    score: float | None


@dataclass
class Cluster:
    cluster_id: str
    members: list[str]        # 记录键（源限定，如 alpha:r-001）
    edges: list = field(default_factory=list)  # 促成该簇的 auto_merge Decision


# ---------- arbitration ----------

# 字段值状态：missing(缺失) / empty(空值) / deleted(源删除) / retracted(来源撤回) / value
ATTR_META_FIELDS = {"id", "src", "ts", "refs", "deleted", "retracted", "_key", "_missing"}


@dataclass
class AttrCandidate:
    value: Any                # 具体值；EMPTY / DELETE 用 state 表达
    state: str                # value | empty | deleted
    weight: float
    sources: list = field(default_factory=list)  # [{key, src, ts, trust}]


@dataclass
class AttrDecision:
    attr: str
    state: str                # chosen | pending | deleted | empty
    value: Any = None
    candidates: list = field(default_factory=list)  # list[AttrCandidate]
    reason: str = ""
    rule_version: str = ""


# ---------- merge / pipeline context ----------

@dataclass
class ClusterPlan:
    cluster_id: str
    entity_id: str
    members: list[str]
    fingerprint: str
    carried: bool = False     # True => 指纹未变，沿用历史裁决与解释


@dataclass
class Context:
    """流水线阶段间共享的上下文（pipeline 只负责按序填充，不含业务判断）。"""
    con: Any
    config: Any
    now: Any
    records: list = field(default_factory=list)       # 活跃记录（含 _key）
    record_by_key: dict = field(default_factory=dict)
    report: Any = None                                  # CandidateReport
    scored: list = field(default_factory=list)        # list[ScoreResult]
    decisions: list = field(default_factory=list)     # list[Decision]
    clusters: list = field(default_factory=list)      # list[Cluster]
    review: list = field(default_factory=list)        # 待审项（匹配层）
    plans: list = field(default_factory=list)         # list[ClusterPlan]
    arbitrations: dict = field(default_factory=dict)  # entity_id -> {attr: AttrDecision}
    ops: list = field(default_factory=list)           # 本代次应用的合并操作
    pending_items: list = field(default_factory=list) # 本代次计算出的待审项（发布时落库）
    changed: bool = False                               # 本代次是否有任何状态变化
    generation: int = 0
