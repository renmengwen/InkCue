"""暖米黄流式白板渲染器的视觉模板注册表。

模板只负责冻结可拼入 generation plan ``globalPrompt`` 的视觉配方；它不决定
scene 划分、不调用图片供应商，也不创建新的批准或恢复状态。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Final, Mapping


DEFAULT_VISUAL_STYLE_PRESET_ID: Final = "warm-paper-minimal-v1"
RENDERER_COMPATIBILITY: Final = "warm-paper-stream-v1"


class VisualStylePresetError(ValueError):
    """请求的视觉模板不存在或模板 ID 非法。"""


def _canonical_text(value: str) -> str:
    """把配方规范化为稳定、可持久化和计算 SHA 的文本。"""

    return "\n".join(line.rstrip() for line in value.strip().splitlines())


def _sha256_text(value: str) -> str:
    return hashlib.sha256(_canonical_text(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VisualStylePreset:
    """一个能被现有暖米黄 stream renderer 消费的视觉模板。"""

    id: str
    display_name: str
    prompt_recipe: str
    recommended_for: tuple[str, ...]
    renderer_compatibility: str
    preview_asset: str

    @property
    def recipe_sha256(self) -> str:
        """规范化 ``prompt_recipe`` 的稳定 SHA-256。"""

        return _sha256_text(self.prompt_recipe)

    def to_dict(self) -> dict[str, object]:
        """返回适合写入 JSON/task/review artifact 的 camelCase 快照。"""

        return {
            "id": self.id,
            "displayName": self.display_name,
            "promptRecipe": self.prompt_recipe,
            "promptRecipeSha256": self.recipe_sha256,
            "recommendedFor": list(self.recommended_for),
            "previewAsset": self.preview_asset,
            "rendererCompatibility": self.renderer_compatibility,
        }


_SCENE_CONTRACT = (
    "输出为 1920×1080，画布固定为 #F5EBD7 暖米黄纸张；画面要适合既有流式落墨与轮廓填色渲染。"
    "每幕围绕一个核心视觉命题组织 1–3 个边界清楚的独立墨迹簇，簇之间保留真实、连续、干净的纸面留白；"
    "除非贯穿结构本身就是不可分割的核心语义，否则不要用道路、长箭头、河流、共同底面或连续背景连接各簇。"
    "每一幕的提示词必须自包含，明确本幕主体、动作或状态、空间关系、造型锚点、构图和配色，"
    "不得写‘沿用上一幕’‘同上’或引用其他图片。允许语义必要的少量画内文字，但必须逐字准确、清晰可读，"
    "不要复刻整句字幕；禁止乱码、意外文字、供应商水印、Logo 和无关品牌标志。"
)


def _recipe(style: str) -> str:
    return _canonical_text(f"{style}{_SCENE_CONTRACT}")


_PRESETS = (
    VisualStylePreset(
        id="warm-paper-minimal-v1",
        display_name="暖米黄极简粗线",
        prompt_recipe=_recipe(
            "使用圆润、有亲和力的粗黑马克笔轮廓，人物和物体高度概括；"
            "只用低饱和橙色与钴蓝色做少量平涂点缀，几乎不使用阴影、纹理和细碎结构，"
            "呈现清爽、易读、像现场快速画出的白板简笔画。"
        ),
        recommended_for=("通用知识解说", "流程说明", "轻量商业表达", "概念科普"),
        renderer_compatibility=RENDERER_COMPATIBILITY,
        preview_asset="assets/visual-style-previews/warm-paper-minimal-v1.svg",
    ),
    VisualStylePreset(
        id="warm-pencil-v1",
        display_name="暖米黄铅笔素描",
        prompt_recipe=_recipe(
            "使用清晰的石墨铅笔主轮廓、克制的轻柔排线和可辨认的深浅笔压；"
            "仅用低饱和赭石与灰蓝做局部色彩提示。保留手工速写气息，但不能用密集纸纹、"
            "大面积阴影或过细碎的交叉线淹没可揭示的主体轮廓。"
        ),
        recommended_for=("人物故事", "个人成长", "品牌叙事", "历史回顾"),
        renderer_compatibility=RENDERER_COMPATIBILITY,
        preview_asset="assets/visual-style-previews/warm-pencil-v1.svg",
    ),
    VisualStylePreset(
        id="guofeng-flat-paper-v1",
        display_name="粗线扁平国风",
        prompt_recipe=_recipe(
            "使用深棕黑色粗轮廓和现代国风扁平造型，以朱红、玉绿、靛青做克制平涂；"
            "可按语义少量使用祥云、山水或书卷式节奏，但不堆砌传统符号，"
            "不采用写实水墨晕染、细密工笔或欧美商务信息图观感。"
        ),
        recommended_for=("传统文化", "历史故事", "东方哲思", "国风科普"),
        renderer_compatibility=RENDERER_COMPATIBILITY,
        preview_asset="assets/visual-style-previews/guofeng-flat-paper-v1.svg",
    ),
    VisualStylePreset(
        id="healing-journal-v1",
        display_name="清新治愈手账",
        prompt_recipe=_recipe(
            "使用圆润轻柔的深灰手绘线条，以鼠尾草绿、蜜桃粉、奶油黄和天蓝色的低饱和色块点缀；"
            "可以加入少量与语义有关的胶带、便签或植物细节，整体温暖、生活化且通透，"
            "避免儿童贴纸堆砌、强烈黑色压迫感和高对比商务图表。"
        ),
        recommended_for=("情绪疗愈", "生活方式", "成长陪伴", "温暖叙事"),
        renderer_compatibility=RENDERER_COMPATIBILITY,
        preview_asset="assets/visual-style-previews/healing-journal-v1.svg",
    ),
    VisualStylePreset(
        id="retro-newspaper-v1",
        display_name="复古报纸拼贴",
        prompt_recipe=_recipe(
            "使用黑色油墨式主轮廓、暗红色强调块、克制的半色调网点和局部撕纸边缘，"
            "形成复古报刊与文化杂志的编辑感。拼贴层次必须保持边界明确，"
            "不得用满版噪点、整页印刷纹理、光滑渐变或密集小字破坏流式揭示。"
        ),
        recommended_for=("社会观察", "人物纪实", "历史档案", "观点评论"),
        renderer_compatibility=RENDERER_COMPATIBILITY,
        preview_asset="assets/visual-style-previews/retro-newspaper-v1.svg",
    ),
    VisualStylePreset(
        id="comic-ink-v1",
        display_name="漫画墨线解释",
        prompt_recipe=_recipe(
            "使用自信且粗细有变化的黑色漫画墨线，灰面和阴影只用稀疏圆点半色调；"
            "黑白灰为主体，暖黄色只强调关键对象，每幕最多再用两种低饱和语义色。"
            "通过具体物件、动作、路径或状态变化解释关系，不靠通用图标、卡片网格或装饰符号凑数；"
            "禁止 3D、摄影写实和光滑渐变。"
        ),
        recommended_for=("机制解释", "问题拆解", "科技科普", "冲突与转折"),
        renderer_compatibility=RENDERER_COMPATIBILITY,
        preview_asset="assets/visual-style-previews/comic-ink-v1.svg",
    ),
)


VISUAL_STYLE_PRESETS: Final[Mapping[str, VisualStylePreset]] = MappingProxyType(
    {preset.id: preset for preset in _PRESETS}
)


def list_visual_style_presets() -> tuple[VisualStylePreset, ...]:
    """按稳定的产品展示顺序枚举全部模板。"""

    return _PRESETS


def get_visual_style_preset(preset_id: str) -> VisualStylePreset:
    """严格查询模板；未知、非字符串或空白 ID 均 fail-closed。"""

    if not isinstance(preset_id, str) or not preset_id.strip():
        raise VisualStylePresetError("visualStylePreset 必须是非空字符串")
    normalized = preset_id.strip()
    try:
        return VISUAL_STYLE_PRESETS[normalized]
    except KeyError as exc:
        supported = ", ".join(VISUAL_STYLE_PRESETS)
        raise VisualStylePresetError(
            f"未知 visualStylePreset：{normalized}；支持：{supported}"
        ) from exc


def resolve_visual_style_preset(preset_id: str | None) -> VisualStylePreset:
    """旧数据缺失（``None``）时采用默认模板；显式非法值仍 fail-closed。"""

    if preset_id is None:
        return get_visual_style_preset(DEFAULT_VISUAL_STYLE_PRESET_ID)
    return get_visual_style_preset(preset_id)


def visual_style_preset_recipe_sha256(
    preset: VisualStylePreset | str,
) -> str:
    """返回模板规范化 prompt 配方的 SHA-256。"""

    resolved = get_visual_style_preset(preset) if isinstance(preset, str) else preset
    if not isinstance(resolved, VisualStylePreset):
        raise VisualStylePresetError("preset 必须是模板 ID 或 VisualStylePreset")
    return resolved.recipe_sha256


def visual_style_preset_catalog_sha256() -> str:
    """返回有序注册表的 canonical JSON SHA，便于诊断版本漂移。"""

    payload = [preset.to_dict() for preset in _PRESETS]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_VISUAL_STYLE_PRESET_ID",
    "RENDERER_COMPATIBILITY",
    "VISUAL_STYLE_PRESETS",
    "VisualStylePreset",
    "VisualStylePresetError",
    "get_visual_style_preset",
    "list_visual_style_presets",
    "resolve_visual_style_preset",
    "visual_style_preset_catalog_sha256",
    "visual_style_preset_recipe_sha256",
]
