from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

EXAMPLES = ROOT / "examples"

from content_source import (  # noqa: E402
    ContentSourceError,
    build_generation_plan,
    build_provisional_cues,
    build_provisional_srt,
    build_source_package,
    content_draft_identity,
    validate_content_draft,
)
from project_workspace import (  # noqa: E402
    ProjectValidationError,
    validate_generation_plan_data,
    validate_pre_project_generation_plan_data,
)
from srt_timeline import parse_srt  # noqa: E402


def topic_draft() -> dict:
    return {
        "schemaVersion": 1,
        "contractVersion": "whiteboard-content-draft-v1",
        "inputMode": "topic",
        "topic": "为什么人会拖延",
        "body": None,
        "rewritePolicy": "generate",
        "targetDurationSeconds": 60,
        "voiceoverMode": "edge-tts",
        "narrationCues": [
            {"cueId": "cue-001", "sceneId": "scene-01", "text": "重要任务出现时,人会先感到压力。"},
            {"cueId": "cue-002", "sceneId": "scene-01", "text": "短暂回避立刻减轻了不舒服。"},
            {"cueId": "cue-003", "sceneId": "scene-02", "text": "大脑因此记住了回避带来的即时奖励。"},
            {"cueId": "cue-004", "sceneId": "scene-02", "text": "把任务缩小到第一步,会更容易重新开始。"},
        ],
        "scenes": [
            {
                "sceneId": "scene-01",
                "name": "压力与回避",
                "coreIdea": "压力触发短暂回避",
                "visualSubject": "人物面对任务后转向轻松活动",
                "imagePrompt": "人物面对写有“任务”的清单后转向轻松活动,主体分区清晰,文字清晰正确。",
            },
            {
                "sceneId": "scene-02",
                "name": "缩小第一步",
                "coreIdea": "用足够小的行动打断回避",
                "visualSubject": "人物沿短小台阶走向任务",
                "imagePrompt": "人物沿着标有数字“1”的很短台阶迈出第一步,留白充足,数字清晰正确。",
            },
        ],
    }


class ContentDraftValidationTests(unittest.TestCase):
    def test_documented_topic_preserve_and_polish_examples_are_valid(self) -> None:
        cases = (
            ("topic-habit-loop-content-draft.json", "topic", "generate"),
            ("text-habit-loop-content-draft.json", "text", "preserve"),
            ("text-habit-loop-polish-content-draft.json", "text", "polish"),
        )
        for filename, input_mode, rewrite_policy in cases:
            with self.subTest(filename=filename):
                draft = json.loads((EXAMPLES / filename).read_text(encoding="utf-8"))
                normalised = validate_content_draft(draft)
                self.assertEqual(normalised["inputMode"], input_mode)
                self.assertEqual(normalised["rewritePolicy"], rewrite_policy)
                self.assertEqual(build_source_package(draft)[0], normalised)

    def test_topic_and_text_policy_matrix_and_edge_only_contract(self) -> None:
        self.assertEqual(validate_content_draft(topic_draft())["rewritePolicy"], "generate")
        text = topic_draft()
        text.update(
            {
                "inputMode": "text",
                "topic": None,
                "body": "甲。乙。丙。丁。",
                "rewritePolicy": "preserve",
            }
        )
        for cue, body in zip(text["narrationCues"], ("甲。", "乙。", "丙。", "丁。")):
            cue["text"] = body
        self.assertEqual(validate_content_draft(text)["body"], "甲。乙。丙。丁。")

        invalid = []
        for mode, policy in (("topic", "preserve"), ("topic", "polish"), ("text", "generate")):
            candidate = copy.deepcopy(text if mode == "text" else topic_draft())
            candidate["rewritePolicy"] = policy
            invalid.append(candidate)
        disabled = topic_draft()
        disabled["voiceoverMode"] = "disabled"
        invalid.append(disabled)
        for candidate in invalid:
            with self.subTest(candidate=(candidate["inputMode"], candidate["rewritePolicy"])), self.assertRaises(ContentSourceError):
                validate_content_draft(candidate)

    def test_nfkc_newline_normalization_keeps_topic_and_body_separate(self) -> None:
        draft = topic_draft()
        draft.update(
            {
                "inputMode": "text",
                "topic": "  ＡＢＣ  ",
                "body": "  第一段\r\n第二段  ",
                "rewritePolicy": "polish",
            }
        )
        draft["narrationCues"][0]["text"] = "  ＡＢＣ\r\n旁白  "
        normalised = validate_content_draft(draft)
        self.assertEqual(normalised["topic"], "ABC")
        self.assertEqual(normalised["body"], "第一段\n第二段")
        self.assertEqual(normalised["narrationCues"][0]["text"], "ABC\n旁白")
        self.assertNotEqual(normalised["topic"], normalised["body"])

    def test_unknown_fields_empty_oversize_duration_and_local_path_are_rejected(self) -> None:
        mutations = []
        unknown = topic_draft(); unknown["apiKey"] = "secret"; mutations.append(unknown)
        empty = topic_draft(); empty["topic"] = "  "; mutations.append(empty)
        long_topic = topic_draft(); long_topic["topic"] = "字" * 201; mutations.append(long_topic)
        for duration in (True, float("nan"), float("inf"), 14.999, 601):
            candidate = topic_draft(); candidate["targetDurationSeconds"] = duration; mutations.append(candidate)
        absolute = topic_draft(); absolute["scenes"][0]["imagePrompt"] = r"读取 C:\private\token"; mutations.append(absolute)
        for index, candidate in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ContentSourceError):
                validate_content_draft(candidate)

    def test_cue_scene_ids_and_contiguous_mapping_are_strict(self) -> None:
        bad_cue = topic_draft(); bad_cue["narrationCues"][1]["cueId"] = "cue-001"
        bad_scene = topic_draft(); bad_scene["scenes"][1]["sceneId"] = "scene-01"
        missing_scene = topic_draft(); missing_scene["narrationCues"][0]["sceneId"] = "scene-99"
        return_scene = topic_draft()
        return_scene["narrationCues"][1]["sceneId"] = "scene-02"
        return_scene["narrationCues"][2]["sceneId"] = "scene-01"
        for candidate in (bad_cue, bad_scene, missing_scene, return_scene):
            with self.assertRaises(ContentSourceError):
                validate_content_draft(candidate)

    def test_image_prompt_must_be_self_contained_for_independent_requests(self) -> None:
        for marker in (
            "延续上一幕的人物与配色",
            "沿用前一张图的构图",
            "同上，只改变人物动作",
            "参照前图保持角色一致",
            "same as previous scene",
        ):
            draft = topic_draft()
            draft["scenes"][1]["imagePrompt"] = (
                f"{marker}，横向暖米黄纸张白板手绘，画内标签清晰正确。"
            )
            with self.subTest(marker=marker), self.assertRaisesRegex(
                ContentSourceError, "独立请求可用的自包含提示词"
            ):
                validate_content_draft(draft)

    def test_text_preserve_keeps_all_letters_numbers_and_order(self) -> None:
        draft = topic_draft()
        draft.update({"inputMode": "text", "body": "版本2,共有30人。", "rewritePolicy": "preserve"})
        draft["narrationCues"] = [
            {"cueId": "cue-001", "sceneId": "scene-01", "text": "版本 2,共有 30 人!"},
            {"cueId": "cue-002", "sceneId": "scene-02", "text": "！？"},
        ]
        validate_content_draft(draft)
        changed = copy.deepcopy(draft)
        changed["narrationCues"][0]["text"] = "版本3,共有30人!"
        with self.assertRaisesRegex(ContentSourceError, "preserve"):
            validate_content_draft(changed)


class DeterministicDerivationTests(unittest.TestCase):
    def test_same_draft_produces_identical_identity_srt_plan_and_manifest(self) -> None:
        first = build_source_package(topic_draft())
        second = build_source_package(copy.deepcopy(topic_draft()))
        self.assertEqual(first, second)
        self.assertEqual(content_draft_identity(topic_draft()), first[3]["contentDraftIdentitySha256"])

    def test_provisional_srt_is_contiguous_and_closes_exactly_at_target(self) -> None:
        draft = topic_draft()
        draft["targetDurationSeconds"] = 15.001
        cues = build_provisional_cues(draft)
        self.assertEqual(cues[0]["startMs"], 0)
        self.assertEqual(cues[-1]["endMs"], 15001)
        self.assertEqual([cue["startMs"] for cue in cues[1:]], [cue["endMs"] for cue in cues[:-1]])
        parsed = parse_srt(build_provisional_srt(draft))
        self.assertEqual(parsed[-1]["endMs"], 15001)
        self.assertTrue(all(cue["durMs"] >= 400 for cue in parsed))

    def test_short_punctuation_many_and_mixed_cues_have_stable_positive_weight(self) -> None:
        draft = topic_draft()
        texts = ["!", "。！？", "A", "中文 English 123"]
        for cue, text in zip(draft["narrationCues"], texts):
            cue["text"] = text
        first = build_provisional_cues(draft)
        second = build_provisional_cues(copy.deepcopy(draft))
        self.assertEqual(first, second)
        self.assertTrue(all(cue["durMs"] >= 400 for cue in first))
        self.assertEqual(first[-1]["endMs"], 60000)

    def test_generation_plan_uses_existing_validator_and_scene_cue_boundaries(self) -> None:
        plan = build_generation_plan(topic_draft())
        validate_generation_plan_data(plan, project_id="")
        self.assertEqual(plan["scenes"][0]["cueRange"], [1, 2])
        self.assertEqual(plan["scenes"][1]["cueRange"], [3, 4])
        self.assertEqual(plan["scenes"][0]["subtitleRange"]["startMs"], 0)
        self.assertEqual(plan["scenes"][-1]["subtitleRange"]["endMs"], 60000)
        self.assertEqual(sum(scene["sceneDurationMs"] for scene in plan["scenes"]), 60000)

    def test_prompt_schema_mapping_is_unique_and_does_not_leak_image_prompt(self) -> None:
        draft = topic_draft()
        plan = build_generation_plan(draft)

        # The coordinator mapping is intentionally boring: same scene/order and
        # byte-for-byte prompt text, with only the formal field name materialized.
        self.assertEqual(
            [scene["prompt"] for scene in plan["scenes"]],
            [scene["imagePrompt"] for scene in draft["scenes"]],
        )
        self.assertEqual(
            [scene["sceneId"] for scene in plan["scenes"]],
            [scene["sceneId"] for scene in draft["scenes"]],
        )
        self.assertTrue(all("imagePrompt" not in scene for scene in plan["scenes"]))

        # A formal plan carrying the content-draft-only field is not a valid
        # substitute for the coordinator mapping.
        invalid = copy.deepcopy(plan)
        invalid["scenes"][0]["imagePrompt"] = invalid["scenes"][0]["prompt"]
        with self.assertRaisesRegex(ProjectValidationError, "imagePrompt"):
            validate_generation_plan_data(invalid, project_id="")
        with self.assertRaisesRegex(ProjectValidationError, "imagePrompt"):
            # The pre-project validator is the storyboard/formal boundary and
            # must fail closed before timing or project creation.
            source = EXAMPLES / "一分钟理解习惯回路.srt"
            candidate = copy.deepcopy(invalid)
            candidate.pop("projectId", None)
            validate_pre_project_generation_plan_data(candidate, source_srt_path=source)


    def test_too_many_cues_for_minimum_readability_is_rejected(self) -> None:
        draft = topic_draft()
        draft["targetDurationSeconds"] = 15
        draft["scenes"] = [draft["scenes"][0]]
        draft["narrationCues"] = [
            {"cueId": f"cue-{index:03d}", "sceneId": "scene-01", "text": "。"}
            for index in range(1, 39)
        ]
        with self.assertRaisesRegex(ContentSourceError, "最短"):
            build_provisional_cues(draft)


if __name__ == "__main__":
    unittest.main()
