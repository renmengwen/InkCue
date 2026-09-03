"""视觉模板的确定性、可解释候选排序。

本模块只给 AI coordinator 提供本地语义信号，不调用外部模型、不读取或写入
项目状态，也不会覆盖用户显式选择。调用方必须把返回的具体 preset ID 冻结到
既有输入和 task 合同中，不能持久化 ``auto``。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import unicodedata
from typing import Final

try:
    from .visual_style_presets import (
        DEFAULT_VISUAL_STYLE_PRESET_ID,
        VisualStylePreset,
        list_visual_style_presets,
    )
except ImportError:  # pragma: no cover - direct script/module execution
    from visual_style_presets import (  # type: ignore
        DEFAULT_VISUAL_STYLE_PRESET_ID,
        VisualStylePreset,
        list_visual_style_presets,
    )


MAX_RECOMMENDATION_LIMIT: Final = 3
_CLEAR_LEAD_MARGIN: Final = 3


class VisualStyleRecommendationError(ValueError):
    """推荐输入或候选数量不符合纯逻辑推荐合同。"""


@dataclass(frozen=True, slots=True)
class VisualStyleRecommendation:
    """一个不包含原文摘录的具体模板推荐。"""

    preset_id: str
    display_name: str
    rationale: str
    score: int
    evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """返回稳定、可直接 JSON 序列化的 camelCase 结构。"""

        return {
            "presetId": self.preset_id,
            "displayName": self.display_name,
            "rationale": self.rationale,
            "score": self.score,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class _SignalRule:
    label: str
    keywords: tuple[str, ...]
    weight: int
    recommended_for: tuple[str, ...]


_SIGNAL_RULES: Final[dict[str, tuple[_SignalRule, ...]]] = {
    "warm-paper-minimal-v1": (
        _SignalRule(
            "步骤与方法",
            ("步骤", "流程", "方法", "指南", "教程", "清单", "怎么做", "入门"),
            3,
            ("流程说明", "通用知识解说"),
        ),
        _SignalRule(
            "通用知识表达",
            ("知识", "概念", "说明", "介绍", "基础", "要点", "总结"),
            2,
            ("通用知识解说", "概念科普"),
        ),
        _SignalRule(
            "轻量商业表达",
            ("产品", "业务", "客户", "团队", "运营", "增长", "方案", "品牌定位"),
            2,
            ("轻量商业表达",),
        ),
    ),
    "warm-pencil-v1": (
        _SignalRule(
            "人物经历",
            ("人物", "人生", "经历", "传记", "童年", "回忆", "故乡", "创业者"),
            4,
            ("人物故事", "历史回顾"),
        ),
        _SignalRule(
            "成长与选择",
            ("成长", "选择", "梦想", "坚持", "改变", "转折", "自我", "初心"),
            3,
            ("个人成长", "人物故事"),
        ),
        _SignalRule(
            "叙事型品牌内容",
            ("品牌故事", "创始", "一路走来", "起源", "匠人", "传承"),
            4,
            ("品牌叙事",),
        ),
    ),
    "guofeng-flat-paper-v1": (
        _SignalRule(
            "传统文化",
            ("传统文化", "国风", "国学", "非遗", "民俗", "书法", "诗词", "茶文化"),
            6,
            ("传统文化", "国风科普"),
        ),
        _SignalRule(
            "中国历史",
            ("古代", "王朝", "朝代", "历史人物", "史记", "盛唐", "宋代", "明清"),
            5,
            ("历史故事",),
        ),
        _SignalRule(
            "东方思想",
            ("东方哲学", "儒家", "道家", "禅意", "阴阳", "天人合一"),
            5,
            ("东方哲思",),
        ),
    ),
    "healing-journal-v1": (
        _SignalRule(
            "情绪与疗愈",
            ("情绪", "焦虑", "疗愈", "治愈", "压力", "内耗", "孤独", "松弛"),
            5,
            ("情绪疗愈",),
        ),
        _SignalRule(
            "生活陪伴",
            ("生活", "陪伴", "家庭", "亲子", "睡眠", "日常", "幸福", "关系"),
            3,
            ("生活方式", "成长陪伴"),
        ),
        _SignalRule(
            "温暖叙事",
            ("温暖", "温柔", "爱", "拥抱", "善意", "希望", "安心"),
            3,
            ("温暖叙事",),
        ),
    ),
    "retro-newspaper-v1": (
        _SignalRule(
            "社会观察",
            ("社会", "现象", "调查", "舆论", "公共", "制度", "群体", "城市"),
            4,
            ("社会观察",),
        ),
        _SignalRule(
            "纪实与档案",
            ("纪实", "档案", "史料", "旧报", "事件回顾", "年代", "见证", "记录"),
            5,
            ("人物纪实", "历史档案"),
        ),
        _SignalRule(
            "观点评论",
            ("评论", "观点", "争议", "反思", "批判", "观察", "深度报道"),
            4,
            ("观点评论",),
        ),
    ),
    "comic-ink-v1": (
        _SignalRule(
            "机制与因果",
            ("机制", "原理", "因果", "为什么", "如何运作", "底层逻辑", "链路"),
            5,
            ("机制解释",),
        ),
        _SignalRule(
            "问题拆解",
            ("问题", "拆解", "故障", "风险", "成本", "冲突", "误区", "陷阱"),
            3,
            ("问题拆解", "冲突与转折"),
        ),
        _SignalRule(
            "科技科普",
            ("科技", "人工智能", "算法", "模型", "系统", "代码", "网络", "数据"),
            4,
            ("科技科普",),
        ),
    ),
}


def _normalise_content(content: str) -> str:
    if not isinstance(content, str):
        raise VisualStyleRecommendationError("content 必须是非空字符串")
    normalised = unicodedata.normalize("NFKC", content).casefold().strip()
    if not normalised:
        raise VisualStyleRecommendationError("content 不能为空")
    return normalised


def _matched_rule(content: str, rule: _SignalRule) -> bool:
    return any(keyword.casefold() in content for keyword in rule.keywords)


def _score_preset(
    content: str,
    preset: VisualStylePreset,
) -> VisualStyleRecommendation:
    labels: list[str] = []
    matched_uses: list[str] = []
    score = 0
    for rule in _SIGNAL_RULES[preset.id]:
        if not _matched_rule(content, rule):
            continue
        score += rule.weight
        labels.append(rule.label)
        matched_uses.extend(
            use for use in rule.recommended_for if use in preset.recommended_for
        )

    # ``recommendedFor`` 既是展示元数据，也是直接短语匹配的弱信号；每项只计一次，
    # 避免正文中重复用词把某个模板无限抬高。
    direct_uses = [
        use for use in preset.recommended_for if use.casefold() in content
    ]
    for use in direct_uses:
        score += 2
        matched_uses.append(use)

    unique_labels = tuple(dict.fromkeys(labels))
    unique_uses = tuple(dict.fromkeys(matched_uses))
    evidence = tuple(f"语义信号：{label}" for label in unique_labels) + tuple(
        f"推荐用途：{use}" for use in unique_uses
    )
    if unique_labels or unique_uses:
        summary = "、".join((*unique_labels, *unique_uses)[:3])
        rationale = f"内容呈现{summary}倾向，与该模板的推荐用途相符。"
    elif preset.id == DEFAULT_VISUAL_STYLE_PRESET_ID:
        rationale = "内容未形成鲜明的专向风格信号，通用极简模板更稳妥。"
    else:
        rationale = "当前内容与该模板的专向语义关联较弱，作为补充候选保留。"
    return VisualStyleRecommendation(
        preset_id=preset.id,
        display_name=preset.display_name,
        rationale=rationale,
        score=score,
        evidence=evidence,
    )


def _rank_recommendations(content: str) -> tuple[VisualStyleRecommendation, ...]:
    presets = list_visual_style_presets()
    registry_order = {preset.id: index for index, preset in enumerate(presets)}
    ranked = sorted(
        (_score_preset(content, preset) for preset in presets),
        key=lambda item: (-item.score, registry_order[item.preset_id]),
    )

    top_score = ranked[0].score
    second_score = ranked[1].score
    if (
        ranked[0].preset_id != DEFAULT_VISUAL_STYLE_PRESET_ID
        and top_score - second_score < _CLEAR_LEAD_MARGIN
    ):
        default = next(
            item
            for item in ranked
            if item.preset_id == DEFAULT_VISUAL_STYLE_PRESET_ID
        )
        conservative_default = replace(
            default,
            score=top_score,
            rationale="多个专向模板的分数没有形成明显差异，优先采用兼容性更稳的通用极简模板。",
            evidence=(*default.evidence, "保守规则：专向分数无明显差异"),
        )
        ranked = [
            conservative_default,
            *(item for item in ranked if item.preset_id != DEFAULT_VISUAL_STYLE_PRESET_ID),
        ]
    return tuple(ranked)


def recommend_visual_style_presets(
    content: str,
    *,
    limit: int = MAX_RECOMMENDATION_LIMIT,
) -> tuple[VisualStyleRecommendation, ...]:
    """稳定返回首选在前的 1–3 个具体模板候选。"""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise VisualStyleRecommendationError("limit 必须是 1–3 的整数")
    if not 1 <= limit <= MAX_RECOMMENDATION_LIMIT:
        raise VisualStyleRecommendationError("limit 必须在 1–3 之间")
    normalised = _normalise_content(content)
    return _rank_recommendations(normalised)[:limit]


def recommend_visual_style_preset(content: str) -> VisualStyleRecommendation:
    """返回单个具体首选；不会返回或持久化 ``auto``。"""

    return recommend_visual_style_presets(content, limit=1)[0]


__all__ = [
    "MAX_RECOMMENDATION_LIMIT",
    "VisualStyleRecommendation",
    "VisualStyleRecommendationError",
    "recommend_visual_style_preset",
    "recommend_visual_style_presets",
]
