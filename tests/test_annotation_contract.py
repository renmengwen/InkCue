from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import annotation_contract  # noqa: E402


class AnnotationContractTests(unittest.TestCase):
    def element(self) -> dict:
        return {
            "sequence": 1,
            "region": {"x": 10, "y": 20, "width": 200, "height": 180},
            "reveal": {
                "startMs": 0,
                "durationMs": 200,
                "protectedRegions": [],
            },
        }

    def test_optional_preview_metadata_is_derived_without_mutating_input(self) -> None:
        authored = [self.element()]
        normalized = annotation_contract.normalize_visual_elements(
            authored,
            canvas={"width": 1920, "height": 1080},
            scene_duration_ms=1000,
        )
        self.assertEqual(normalized[0]["_previewLabel"], "元素 1")
        self.assertEqual(normalized[0]["_previewHandPath"], ((30, 110), (190, 110)))
        self.assertNotIn("label", authored[0])
        self.assertNotIn("handPath", authored[0])
        self.assertNotIn("direction", authored[0]["reveal"])

    def test_authored_label_and_hand_path_are_preserved_for_preview(self) -> None:
        element = self.element()
        element["label"] = "主体"
        element["handPath"] = {"start": [20, 30], "end": [200, 180]}
        normalized = annotation_contract.normalize_visual_elements([element])
        self.assertEqual(normalized[0]["_previewLabel"], "主体")
        self.assertEqual(normalized[0]["_previewHandPath"], ((20, 30), (200, 180)))

    def test_unknown_element_reveal_and_region_fields_fail(self) -> None:
        for mutate, expected in (
            (lambda e: e.update({"summary": "not allowed"}), "未知字段"),
            (lambda e: e["reveal"].update({"speed": "fast"}), "未知字段"),
            (lambda e: e["region"].update({"right": 210}), "未知字段"),
        ):
            with self.subTest(expected=expected):
                element = self.element()
                mutate(element)
                with self.assertRaisesRegex(annotation_contract.AnnotationContractError, expected):
                    annotation_contract.validate_visual_elements([element])

    def test_direction_sequence_region_and_reveal_are_strict(self) -> None:
        invalids = []
        bad_direction = self.element()
        bad_direction["reveal"]["direction"] = "diagonal"
        invalids.append((bad_direction, "direction"))
        bad_sequence = self.element()
        bad_sequence["sequence"] = 2
        invalids.append((bad_sequence, "sequence"))
        bad_region = self.element()
        bad_region["region"]["width"] = 0
        invalids.append((bad_region, "正向非空区域"))
        bad_reveal = self.element()
        bad_reveal["reveal"]["durationMs"] = 0
        invalids.append((bad_reveal, "正时长"))
        for element, expected in invalids:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(annotation_contract.AnnotationContractError, expected):
                    annotation_contract.validate_visual_elements([element])

    def test_canvas_timing_overlap_and_max_element_rules(self) -> None:
        outside = self.element()
        outside["region"]["x"] = 1900
        with self.assertRaisesRegex(annotation_contract.AnnotationContractError, "canvas"):
            annotation_contract.validate_visual_elements(
                [outside], canvas={"width": 1920, "height": 1080}
            )

        first = self.element()
        second = self.element()
        second["sequence"] = 2
        second["reveal"]["startMs"] = 100
        with self.assertRaisesRegex(annotation_contract.AnnotationContractError, "不重叠"):
            annotation_contract.validate_visual_elements([first, second])

        elements = []
        for index in range(4):
            element = self.element()
            element["sequence"] = index + 1
            element["reveal"]["startMs"] = index * 200
            elements.append(element)
        with self.assertRaisesRegex(annotation_contract.AnnotationContractError, "最多允许 3"):
            annotation_contract.validate_visual_elements(elements)

    def test_legacy_formal_normalizer_is_explicit_and_keeps_more_than_three_elements(self) -> None:
        elements = []
        for index in range(4):
            element = self.element()
            element["sequence"] = index + 1
            element["type"] = "structure"
            element["reveal"]["startMs"] = index * 2000
            element["reveal"]["durationMs"] = 500
            element["reveal"]["direction"] = "top_to_bottom"
            element["reveal"]["maskPaddingPx"] = 18
            elements.append(element)
        normalized = annotation_contract.normalize_legacy_visual_elements(
            elements,
            canvas={"width": 1920, "height": 1080},
            scene_duration_ms=10000,
        )
        self.assertEqual(len(normalized), 4)
        self.assertNotIn("type", normalized[0])
        self.assertEqual(normalized[0]["reveal"]["direction"], "top-to-bottom")
        with self.assertRaises(annotation_contract.AnnotationContractError):
            annotation_contract.validate_visual_elements(elements)


if __name__ == "__main__":
    unittest.main()
