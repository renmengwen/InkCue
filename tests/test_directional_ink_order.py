from __future__ import annotations

import ast
import inspect
import sys
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import render_stream_whiteboard  # noqa: E402
import stream_render  # noqa: E402


DIRECTIONS = (
    "left-to-right",
    "right-to-left",
    "top-to-bottom",
    "bottom-to-top",
)


def _primary_coordinate(point: tuple[float, float], direction: str) -> float:
    """把四方向统一成“数值越大，绘制进度越靠后”的主轴。"""

    row, col = point
    if direction == "left-to-right":
        return float(col)
    if direction == "right-to-left":
        return -float(col)
    if direction == "top-to-bottom":
        return float(row)
    if direction == "bottom-to-top":
        return -float(row)
    raise AssertionError(f"未覆盖的测试方向: {direction}")


def _active_with_dense_trailing_detail(direction: str) -> np.ndarray:
    """主体轮廓横跨画面，但指定方向末端故意放置高密度细节。"""

    horizontal = direction in {"left-to-right", "right-to-left"}
    active = np.zeros((11, 18), dtype=bool) if horizontal else np.zeros((18, 11), dtype=bool)
    if horizontal:
        active[5, 1:17] = True
        trailing_cols = range(13, 17) if direction == "left-to-right" else range(1, 5)
        for col in trailing_cols:
            active[1:10, col] = True
    else:
        active[1:17, 5] = True
        trailing_rows = range(13, 17) if direction == "top-to-bottom" else range(1, 5)
        for row in trailing_rows:
            active[row, 1:10] = True
    return active


def _assert_directional_progress(
    testcase: unittest.TestCase,
    points: list[tuple[float, float]],
    direction: str,
) -> None:
    testcase.assertGreaterEqual(len(points), 4)
    progress = [_primary_coordinate(point, direction) for point in points]
    span = max(progress) - min(progress)
    testcase.assertGreater(span, 0)

    # 首笔必须位于指定方向的前缘 25%，不能再从远端高密度头发/纹理开局。
    testcase.assertLessEqual(
        progress[0],
        min(progress) + span * 0.25,
        f"{direction} 首笔没有从指定方向前缘开始: {points[0]}",
    )

    # 允许一个方向带内部连续游走，但总体时间分位必须朝目标方向推进。
    chunk = max(2, len(progress) // 5)
    early_mean = sum(progress[:chunk]) / chunk
    late_mean = sum(progress[-chunk:]) / chunk
    testcase.assertLess(
        early_mean,
        late_mean,
        f"{direction} 的整体路径发生全局反向或没有方向性进度",
    )


class DirectionalGridOrderTests(unittest.TestCase):
    def test_grid_path_obeys_all_four_directions_and_does_not_start_at_dense_tail(self) -> None:
        for direction in DIRECTIONS:
            with self.subTest(direction=direction):
                active = _active_with_dense_trailing_detail(direction)
                streams = stream_render.cluster_ink_streams(active, direction=direction)
                path = stream_render.flatten_streams(streams)

                expected = {tuple(point) for point in np.argwhere(active)}
                self.assertEqual(set(path), expected)
                self.assertEqual(len(path), len(expected))
                _assert_directional_progress(self, path, direction)

    def test_grid_default_is_explicit_left_to_right_for_backward_compatibility(self) -> None:
        active = _active_with_dense_trailing_detail("left-to-right")
        self.assertEqual(
            stream_render.cluster_ink_streams(active),
            stream_render.cluster_ink_streams(active, direction="left-to-right"),
        )


class DirectionalSkeletonOrderTests(unittest.TestCase):
    @staticmethod
    def _strokes(direction: str) -> list[list[tuple[float, float]]]:
        positions = (12.0, 36.0, 60.0, 84.0)
        if direction in {"left-to-right", "right-to-left"}:
            # 输入故意倒序，且笔画自身朝向交错，排序不能依赖输入偶然顺序。
            return [
                [(x, 30.0), (x, 10.0)] if index % 2 else [(x, 10.0), (x, 30.0)]
                for index, x in reversed(list(enumerate(positions)))
            ]
        return [
            [(30.0, y), (10.0, y)] if index % 2 else [(10.0, y), (30.0, y)]
            for index, y in reversed(list(enumerate(positions)))
        ]

    @staticmethod
    def _stroke_center_as_row_col(
        stroke: list[tuple[float, float]],
    ) -> tuple[float, float]:
        # 骨架点是 (x, y)，统一转换为 grid 测试使用的 (row, col)。
        x = sum(point[0] for point in stroke) / len(stroke)
        y = sum(point[1] for point in stroke) / len(stroke)
        return (y, x)

    def test_skeleton_strokes_obey_all_four_directions(self) -> None:
        for direction in DIRECTIONS:
            with self.subTest(direction=direction):
                source = self._strokes(direction)
                ordered = stream_render._order_skeleton_strokes(
                    source,
                    direction=direction,
                )
                centers = [self._stroke_center_as_row_col(stroke) for stroke in ordered]
                progress = [_primary_coordinate(point, direction) for point in centers]

                self.assertEqual(len(ordered), len(source))
                self.assertEqual(progress, sorted(progress))
                _assert_directional_progress(self, centers, direction)

    def test_skeleton_default_is_explicit_left_to_right_for_backward_compatibility(self) -> None:
        strokes = self._strokes("left-to-right")
        self.assertEqual(
            stream_render._order_skeleton_strokes(strokes),
            stream_render._order_skeleton_strokes(strokes, direction="left-to-right"),
        )


class FormalRendererDirectionPassThroughTests(unittest.TestCase):
    def test_region_grid_path_passes_direction_to_grid_orderer(self) -> None:
        renderer = render_stream_whiteboard.RegionStreamRenderer.__new__(
            render_stream_whiteboard.RegionStreamRenderer
        )
        renderer.cfg = SimpleNamespace(grid_edge=1)
        renderer.active_all = np.ones((3, 4), dtype=bool)
        allowed = np.ones((3, 4), dtype=bool)
        sentinel = [[(0, 0), (0, 1)]]

        with mock.patch.object(
            render_stream_whiteboard.sr,
            "cluster_ink_streams",
            return_value=sentinel,
        ) as orderer:
            result = renderer._region_grid_path(allowed, direction="right-to-left")

        self.assertEqual(result, [(0, 0), (0, 1)])
        args, kwargs = orderer.call_args
        self.assertTrue(np.array_equal(args[0], renderer.active_all))
        passed_direction = kwargs.get("direction", args[1] if len(args) > 1 else None)
        self.assertEqual(passed_direction, "right-to-left")

    def test_region_skeleton_path_passes_direction_to_stroke_orderer(self) -> None:
        renderer = render_stream_whiteboard.RegionStreamRenderer.__new__(
            render_stream_whiteboard.RegionStreamRenderer
        )
        renderer.cfg = SimpleNamespace(
            skeleton_min_points=2,
            skeleton_resample_spacing=1.0,
        )
        renderer.ink_pixels = np.ones((4, 4), dtype=bool)
        allowed = np.ones((4, 4), dtype=bool)
        raw = [[(0, 0), (1, 0), (2, 0), (3, 0)]]
        sentinel = [[(90, 90), (91, 90)]]

        with mock.patch.object(
            render_stream_whiteboard.sr,
            "_zhang_suen_skeleton",
            return_value=np.ones((4, 4), dtype=bool),
        ), mock.patch.object(
            render_stream_whiteboard.sr,
            "trace_8connected",
            return_value=raw,
        ), mock.patch.object(
            render_stream_whiteboard.sr,
            "_resample_stroke_points",
            side_effect=lambda points, _spacing: points,
        ), mock.patch.object(
            render_stream_whiteboard.sr,
            "_chaikin_smooth",
            side_effect=lambda points, iterations=1: points,
        ), mock.patch.object(
            render_stream_whiteboard.sr,
            "_order_skeleton_strokes",
            return_value=sentinel,
        ) as orderer:
            result = renderer._region_skeleton_strokes(
                allowed,
                direction="bottom-to-top",
            )

        self.assertEqual(result, sentinel)
        args, kwargs = orderer.call_args
        self.assertEqual(len(args[0]), 1)
        passed_direction = kwargs.get("direction", args[1] if len(args) > 1 else None)
        self.assertEqual(passed_direction, "bottom-to-top")

    def test_formal_render_loop_does_not_fall_back_to_helper_defaults(self) -> None:
        """正式循环必须消费 reveal.direction，而非只让底层接口拥有默认值。"""

        tree = ast.parse(textwrap.dedent(inspect.getsource(
            render_stream_whiteboard.RegionStreamRenderer.render_to
        )))
        relevant_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"_region_grid_path", "_region_skeleton_strokes"}
        ]
        self.assertGreaterEqual(len(relevant_calls), 2)

        assigned_from_reveal = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if any(isinstance(target, ast.Name) and target.id == "direction" for target in targets):
                rendered = ast.dump(value)
                if "reveal" in rendered and "direction" in rendered:
                    assigned_from_reveal = True

        for call in relevant_calls:
            direction_values = [
                keyword.value for keyword in call.keywords if keyword.arg == "direction"
            ]
            if len(call.args) >= 2:
                direction_values.append(call.args[1])
            self.assertTrue(
                direction_values,
                f"{call.func.attr} 在正式 render_to 中依赖了默认方向",
            )
            rendered_values = " ".join(ast.dump(value) for value in direction_values)
            self.assertTrue(
                "direction" in rendered_values,
                f"{call.func.attr} 没有接收 reveal.direction 派生值",
            )

        self.assertTrue(assigned_from_reveal, "render_to 没有读取 reveal['direction']")


if __name__ == "__main__":
    unittest.main()
