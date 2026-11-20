"""集中配置：路径 + 匹配/裁决规则参数（规则换版通过 rule_version 体现）。"""
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "input.json"
DB_PATH = ROOT / "recordlink.db"

RULE_VERSION = "1.0.0"


@dataclass(frozen=True)
class Config:
    """一次流水线运行使用的全部规则参数。

    任何参数变化都视为“规则换版”：指纹不变的实体自动沿用历史裁决，
    指纹变化的实体只重算受影响部分。
    """

    rule_version: str = RULE_VERSION
    # 匹配阈值：>= auto 且无强冲突才自动合并；>= review 进入人工待审。
    auto_merge_threshold: float = 0.85
    review_threshold: float = 0.55
    # 冲突判定：冲突字段双方都有值且相似度低于该值 => 强冲突。
    conflict_threshold: float = 0.3
    # 候选生成：桶超过该大小则跳过展开并在报告中说明（控制计算成本）。
    max_bucket_size: int = 500
    blocking_keys: tuple = ("name_prefix", "status")
    # 评分字段权重与冲突字段（冲突字段双方有值且不一致时阻断自动合并）。
    field_weights: dict = field(default_factory=lambda: {
        "name": 3.0, "address": 3.0, "value": 1.0, "status": 1.0,
    })
    conflict_fields: tuple = ("address",)
    # 裁决：字段类型（scalar 取权重最大 / latest 取最新 / set 取并集）。
    field_types: dict = field(default_factory=lambda: {
        "name": "scalar", "address": "scalar", "value": "scalar", "status": "scalar",
    })
    source_trust: dict = field(default_factory=lambda: {"alpha": 0.9, "beta": 0.8})
    default_trust: float = 0.5
    freshness_half_life_days: float = 180.0
    # 证据不足判定：次优候选权重 / 最优权重 > 该比例 => 保留待审。
    pending_margin: float = 0.9

    def signature(self) -> dict:
        return asdict(self)


def load(overrides: dict | None = None) -> Config:
    cfg = Config()
    if not overrides:
        return cfg
    valid = {f for f in Config.__dataclass_fields__}
    unknown = set(overrides) - valid
    if unknown:
        raise ValueError(f"未知配置项: {sorted(unknown)}")
    return Config(**{**asdict(cfg), **overrides})
