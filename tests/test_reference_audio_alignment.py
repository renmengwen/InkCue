from __future__ import annotations

import unittest

from scripts.reference_audio_alignment import (
    ReferenceAlignmentError,
    align_reference_audio,
    normalise_alignment_text,
)


def srt(cues: list[tuple[int, int, str]]) -> str:
    def timestamp(milliseconds: int) -> str:
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    return "\n\n".join(
        f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{text}"
        for index, (start, end, text) in enumerate(cues, start=1)
    )


class ReferenceAudioAlignmentTests(unittest.TestCase):
    def assert_contract(self, result: dict, reference: str, duration_ms: int) -> None:
        cues = result["cues"]
        self.assertEqual(cues[0]["startMs"], 0)
        self.assertEqual(cues[-1]["endMs"], duration_ms)
        self.assertEqual(
            normalise_alignment_text("".join(cue["text"] for cue in cues)),
            normalise_alignment_text(
                "".join(block.split("\n", 2)[2] for block in reference.split("\n\n"))
            ),
        )
        self.assertEqual(
            "".join(cue["text"] for cue in cues),
            "".join(block.split("\n", 2)[2] for block in reference.split("\n\n")),
        )
        for cue in cues:
            self.assertGreater(cue["endMs"], cue["startMs"])
            self.assertEqual(cue["sourceCueRange"], [cue["sourceCueOrdinal"]] * 2)
        for left, right in zip(cues, cues[1:]):
            self.assertEqual(left["endMs"], right["startMs"])
        scenes = result["scenes"]
        self.assertEqual(scenes[0]["startMs"], 0)
        self.assertEqual(scenes[-1]["endMs"], duration_ms)
        for left, right in zip(scenes, scenes[1:]):
            self.assertEqual(left["endMs"], right["startMs"])

    def test_wrong_missing_extra_words_and_different_sentence_splits(self) -> None:
        reference = srt(
            [
                (0, 1000, "为什么我们总把爱咽回去？"),
                (1000, 2000, "很多人以为，这是性格差异。"),
                (2000, 3000, "其实，这是刻在骨子里的生存记忆。"),
            ]
        )
        asr = srt(
            [
                (210, 1110, "为什么我们总把爱咽回去打最后几滴"),
                (1300, 2100, "很多人以为这是性格差异"),
                (2500, 3150, "其实这是刻在骨子里的"),
                (3500, 4400, "生存记忆"),
            ]
        )
        result = align_reference_audio(
            reference,
            asr,
            [
                {"sceneId": "scene-01", "cueRange": [1, 2]},
                {"sceneId": "scene-02", "cueRange": [3, 3]},
            ],
            4600,
            min_match_ratio=0.6,
            max_normalized_edit_ratio=0.45,
        )

        self.assert_contract(result, reference, 4600)
        self.assertEqual("".join(cue["text"] for cue in result["cues"]), "为什么我们总把爱咽回去？很多人以为，这是性格差异。其实，这是刻在骨子里的生存记忆。")
        self.assertNotIn("打最后几滴", "".join(cue["text"] for cue in result["cues"]))
        self.assertEqual([scene["sceneId"] for scene in result["scenes"]], ["scene-01", "scene-02"])
        self.assertFalse(result["diagnostics"]["timingFallbackUsed"])

    def test_one_source_cue_can_split_into_multiple_acoustic_cues_without_crossing_scene(self) -> None:
        reference = srt(
            [
                (0, 1000, "先看见问题，然后理解原因，最后采取行动。"),
                (1000, 2000, "这就是完整的方法。"),
            ]
        )
        asr = srt(
            [
                (100, 700, "先看见问题"),
                (900, 1500, "然后理解原因"),
                (1800, 2400, "最后采取行动"),
                (2800, 3600, "这就是完整的方法"),
            ]
        )
        result = align_reference_audio(
            reference,
            asr,
            [
                {"sceneId": "scene-a", "sourceCueRange": [1, 1]},
                {"sceneId": "scene-b", "sourceCueRange": [2, 2]},
            ],
            3800,
        )

        self.assert_contract(result, reference, 3800)
        first_source_pieces = [cue for cue in result["cues"] if cue["sourceCueOrdinal"] == 1]
        self.assertGreaterEqual(len(first_source_pieces), 2)
        self.assertEqual({cue["sceneId"] for cue in first_source_pieces}, {"scene-a"})
        self.assertEqual(result["scenes"][0]["endMs"], result["scenes"][1]["startMs"])

    def test_punctuation_only_asr_cue_is_ignored_but_natural_pause_is_preserved_in_timing(self) -> None:
        reference = srt([(0, 1000, "第一句话。"), (1000, 2000, "第二句话。")])
        asr = srt(
            [
                (200, 800, "第一句话"),
                (900, 1200, "……"),
                (1800, 2400, "第二句话"),
                (2450, 2500, "。"),
            ]
        )
        result = align_reference_audio(
            reference,
            asr,
            [{"sceneId": "scene", "cueRange": [1, 2]}],
            2600,
        )

        self.assert_contract(result, reference, 2600)
        self.assertEqual(result["diagnostics"]["ignoredPunctuationAsrCueOrdinals"], [2, 4])
        self.assertEqual(result["cues"][0]["endMs"], 1800)

    def test_twenty_four_reference_cues_against_fifty_one_asr_cues(self) -> None:
        phrases = [f"第{index}段讲清楚一个重点。" for index in range(1, 25)]
        reference = srt(
            [(index * 1000, (index + 1) * 1000, phrase) for index, phrase in enumerate(phrases)]
        )
        asr_cues: list[tuple[int, int, str]] = []
        cursor = 120
        for phrase in phrases:
            midpoint = max(1, len(phrase) // 2)
            for part in (phrase[:midpoint], phrase[midpoint:]):
                asr_cues.append((cursor, cursor + 300, part))
                cursor += 430
        asr_cues.extend(
            [
                (cursor, cursor + 100, "。"),
                (cursor + 150, cursor + 250, "……"),
                (cursor + 300, cursor + 400, "！"),
            ]
        )
        scenes = [
            {"sceneId": f"scene-{index // 4 + 1:02d}", "cueRange": [index + 1, index + 4]}
            for index in range(0, 24, 4)
        ]
        result = align_reference_audio(reference, srt(asr_cues), scenes, cursor + 600)

        self.assert_contract(result, reference, cursor + 600)
        self.assertEqual(result["diagnostics"]["referenceCueCount"], 24)
        self.assertEqual(result["diagnostics"]["asrCueCount"], 51)
        self.assertEqual(result["diagnostics"]["lexicalAsrCueCount"], 48)
        self.assertEqual(len(result["scenes"]), 6)
        self.assertTrue(all(cue["sourceCueRange"][0] == cue["sourceCueRange"][1] for cue in result["cues"]))

    def test_low_quality_alignment_fails_closed_with_diagnostics(self) -> None:
        reference = srt([(0, 1000, "这是已经确认的完整旁白内容。")])
        asr = srt([(0, 1000, "天气预报说明天可能下雨。")])
        with self.assertRaises(ReferenceAlignmentError) as caught:
            align_reference_audio(
                reference,
                asr,
                [{"sceneId": "scene", "cueRange": [1, 1]}],
                1100,
            )
        self.assertEqual(caught.exception.diagnostics["status"], "FAIL")
        self.assertIn("matchRatio", caught.exception.diagnostics)
        self.assertFalse(caught.exception.diagnostics["timingFallbackUsed"])

    def test_too_few_acoustic_boundaries_fails_instead_of_proportional_timing(self) -> None:
        reference = srt(
            [
                (0, 1000, "第一段。"),
                (1000, 2000, "第二段。"),
                (2000, 3000, "第三段。"),
            ]
        )
        asr = srt([(100, 2900, "第一段第二段第三段")])
        with self.assertRaises(ReferenceAlignmentError) as caught:
            align_reference_audio(
                reference,
                asr,
                [{"sceneId": "scene", "cueRange": [1, 3]}],
                3000,
                min_match_ratio=0.5,
            )
        self.assertEqual(caught.exception.diagnostics["requiredSourceCueBoundaries"], 2)
        self.assertEqual(caught.exception.diagnostics["availableInternalAcousticBoundaries"], 0)
        self.assertFalse(caught.exception.diagnostics["timingFallbackUsed"])

    def test_invalid_scene_coverage_and_asr_past_audio_duration_are_rejected(self) -> None:
        reference = srt([(0, 1000, "第一段。"), (1000, 2000, "第二段。")])
        asr = srt([(0, 900, "第一段"), (1000, 2100, "第二段")])
        with self.assertRaisesRegex(ReferenceAlignmentError, "未连续覆盖"):
            align_reference_audio(
                reference,
                asr,
                [{"sceneId": "scene", "cueRange": [1, 1]}],
                2200,
            )
        with self.assertRaisesRegex(ReferenceAlignmentError, "超出音频总时长"):
            align_reference_audio(
                reference,
                asr,
                [{"sceneId": "scene", "cueRange": [1, 2]}],
                2000,
            )

    def test_rejects_short_text_that_wrongly_consumes_most_of_long_audio(self) -> None:
        reference = srt(
            [
                (0, 1000, "开场很短。"),
                (1000, 2000, "后面的旁白内容明显更长，应当拥有正常的声学时间范围。"),
            ]
        )
        asr = srt(
            [
                (0, 59_900, "开场很短"),
                (60_000, 69_500, "后面的旁白内容明显更长应当拥有正常的声学时间范围"),
            ]
        )
        with self.assertRaisesRegex(ReferenceAlignmentError, "声学边界明显失真") as caught:
            align_reference_audio(
                reference,
                asr,
                [{"sceneId": "scene", "cueRange": [1, 2]}],
                70_000,
            )
        plausibility = caught.exception.diagnostics["timingPlausibility"]
        self.assertEqual(caught.exception.diagnostics["status"], "FAIL")
        self.assertEqual(plausibility["implausibleSourceCues"][0]["sourceCueOrdinal"], 1)
        self.assertIn(
            "disproportionate_track_share",
            plausibility["implausibleSourceCues"][0]["reasons"],
        )


if __name__ == "__main__":
    unittest.main()
