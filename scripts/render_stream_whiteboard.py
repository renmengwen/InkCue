#!/usr/bin/env python3
"""
SRT 白板动画 - 整合渲染器（mask 编排 + stream 画法）

把一张线稿图 + 同名 annotation.json 渲染成白板手绘动画：
  - 编排沿用 whiteboard-mask-animation：按 sequence/startMs 顺序逐区域揭示，
    每个区域的可作画范围 = 矩形 region 扣除「后续区域 + protectedRegions」，
    未开始的区域因掩码限制不会提前露线（mask 的核心不变量）。
  - 画法换成 whiteboard-stream-animation：每个区域在自己的允许掩码内，
    沿骨架/网格笔迹连续落墨（起笔 ink → 添彩 color），笔尖跟随真实笔迹，
    所有区域共享同一张持久画布，已画完的区域保留在画布上。

与 mask 的矩形擦除揭示不同：这里是「笔尖沿线滑行、边走边落墨」的连贯笔迹。
输出末行打印 OUTPUT=<路径>，便于上层捕获。

用法：
  <ENV_PY> render_stream_whiteboard.py <图片> <标注json> <输出mp4> [手部素材png]
  可选参数见 --help（--ink-path / --color-fill / --pause / --total-ms 等）。
  --total-ms 缺省时用标注里的 sceneDurationMs。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import math
import multiprocessing
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

# 复用 stream 渲染器的全部构件（同目录）
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
import stream_render as sr  # noqa: E402
import bounded_execution  # noqa: E402
import ffmpeg_frame_sink  # noqa: E402
import media_validation  # noqa: E402
import project_workspace  # noqa: E402
import render_timing  # noqa: E402
import annotation_review  # noqa: E402

# 内置素材的马克笔杆包含用户批准的版权标识 @moveR；不得静默替换为无字版本。
DEFAULT_HAND = _SCRIPT_DIR.parent / "assets" / "drawing-hand.png"


# ──────────────────────────────────────────────────────────────
# 区域几何：把标注画布坐标缩放到输出尺寸
# ──────────────────────────────────────────────────────────────
def _scaled_rect(region: dict, sx: float, sy: float, out_w: int, out_h: int) -> tuple[int, int, int, int]:
    x0 = int(round(region["x"] * sx))
    y0 = int(round(region["y"] * sy))
    x1 = int(round((region["x"] + region["width"]) * sx))
    y1 = int(round((region["y"] + region["height"]) * sy))
    x0 = max(0, min(out_w, x0))
    x1 = max(0, min(out_w, x1))
    y0 = max(0, min(out_h, y0))
    y1 = max(0, min(out_h, y1))
    return x0, y0, x1, y1


def _frame_progress_indices(n_steps: int, target_frames: int) -> list[int]:
    """把 n_steps 个笔尖位置均匀映射到 target_frames 帧。"""
    if n_steps == 0 or target_frames <= 0:
        return []
    if target_frames == 1:
        return [n_steps - 1]
    return [round(f * (n_steps - 1) / (target_frames - 1)) for f in range(target_frames)]


# ──────────────────────────────────────────────────────────────
# 每区域的 stream 笔迹渲染，写入共享持久画布
# ──────────────────────────────────────────────────────────────
class RegionStreamRenderer:
    """持有整段渲染的共享状态；逐区域把 stream 笔迹画进同一张画布。"""

    def __init__(self, image_bgr: np.ndarray, annotation: dict, cfg: sr.Config,
                 hand_png: Path | None, bare_tip: bool,
                 output_size: tuple[int, int] | None = None,
                 preloaded_hand: tuple[np.ndarray, np.ndarray] | None = None) -> None:
        self.cfg = cfg
        self.ann = annotation
        self.canvas_bgr = sr._hex_to_bgr(cfg.canvas_hex)

        # 输出尺寸：长边限到 cap，对齐到 grid_edge 的偶数倍（编码要求偶数）
        h0, w0 = image_bgr.shape[:2]
        if output_size is not None:
            w, h = output_size
        else:
            scale = cfg.cap_long_edge / max(h0, w0)
            align = cfg.grid_edge if cfg.grid_edge % 2 == 0 else cfg.grid_edge * 2
            w = max(align, (int(round(w0 * scale)) // align) * align)
            h = max(align, (int(round(h0 * scale)) // align) * align)
        self.out_w, self.out_h = w, h

        # 标注画布坐标 → 输出坐标的缩放比
        cw = annotation["canvas"]["width"]
        ch = annotation["canvas"]["height"]
        self.sx = self.out_w / cw
        self.sy = self.out_h / ch

        self.color_img = cv2.resize(image_bgr, (self.out_w, self.out_h), interpolation=cv2.INTER_AREA)
        self.color_img = sr.normalize_paper_background(self.color_img, self.canvas_bgr, cfg)
        gray = cv2.cvtColor(self.color_img, cv2.COLOR_BGR2GRAY)
        self.thresh_map = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 10
        )
        self.grid_blocks = sr._to_grid_blocks(self.thresh_map, cfg.grid_edge)
        self.active_all = sr._active_mask(self.thresh_map, cfg.grid_edge, cfg.ink_threshold)
        self.ink_pixels = self.thresh_map < cfg.ink_threshold
        self.ink_paint = np.repeat(self.thresh_map[:, :, None], 3, axis=2).astype(np.float32)

        # 共享持久画布
        self.drawn = np.empty((self.out_h, self.out_w, 3), dtype=np.float32)
        self.drawn[...] = self.canvas_bgr.astype(np.float32)

        # 笔尖覆盖
        self.tip: sr.TipOverlay | None = None
        if not bare_tip:
            hand_data = preloaded_hand
            if hand_data is None and hand_png:
                hand_data = sr._load_hand(hand_png, cfg.target_hand_height)
            ax, ay = cfg.tip_anchor_x, cfg.tip_anchor_y
            if hand_data is None:
                hand_data = sr._procedural_tip(cfg.target_hand_height)
                ax, ay = 0.5, 0.70
            self.tip = sr.TipOverlay(hand_data[0], hand_data[1], tip_anchor_x=ax, tip_anchor_y=ay)

    def _cell_center(self, cell: tuple[int, int]) -> tuple[int, int]:
        r, c = cell
        e = self.cfg.grid_edge
        return (c * e + e // 2, r * e + e // 2)

    def _snapshot_with_tip(self, px: int, py: int) -> np.ndarray:
        snap = self.drawn.astype(np.uint8)
        if self.tip is not None:
            self.tip.stamp(snap, px, py)
        return snap

    # ── 单区域的允许掩码：矩形 - 后续区域 - protectedRegions ──
    def _allowed_mask(self, element: dict, later_elements: list[dict]) -> np.ndarray:
        mask = np.zeros((self.out_h, self.out_w), dtype=bool)
        x0, y0, x1, y1 = _scaled_rect(element["region"], self.sx, self.sy, self.out_w, self.out_h)
        mask[y0:y1, x0:x1] = True
        for later in later_elements:
            lx0, ly0, lx1, ly1 = _scaled_rect(later["region"], self.sx, self.sy, self.out_w, self.out_h)
            mask[ly0:ly1, lx0:lx1] = False
        for prot in element.get("reveal", {}).get("protectedRegions", []):
            px0, py0, px1, py1 = _scaled_rect(prot, self.sx, self.sy, self.out_w, self.out_h)
            mask[py0:py1, px0:px1] = False
        return mask

    # ── 区域内笔迹路径 ──
    def _region_grid_path(
        self,
        allowed: np.ndarray,
        direction: str,
    ) -> list[tuple[int, int]]:
        """网格模式：把区域内含墨的格聚类并串成连续格路径。"""
        allowed_u8 = allowed.astype(np.uint8)
        allowed_cell = sr._to_grid_blocks(allowed_u8, self.cfg.grid_edge).any(axis=(2, 3))
        active = self.active_all & allowed_cell
        if not active.any():
            return []
        streams = sr.cluster_ink_streams(active, direction=direction)
        return sr.flatten_streams(streams)

    def _region_skeleton_strokes(
        self,
        allowed: np.ndarray,
        direction: str,
    ) -> list[list[tuple[int, int]]]:
        """骨架模式：区域内墨迹细化 + 8 邻接追踪 + 重采样平滑。"""
        cfg = self.cfg
        region_ink = self.ink_pixels & allowed
        if not region_ink.any():
            return []
        skel = sr._zhang_suen_skeleton(region_ink, max_iterations=160)
        raw = sr.trace_8connected(skel, min_points=cfg.skeleton_min_points)
        if not raw:
            return []
        spacing = cfg.skeleton_resample_spacing
        out: list[list[tuple[int, int]]] = []
        for stroke in raw:
            pts = [(float(x), float(y)) for x, y in stroke]
            pts = sr._resample_stroke_points(pts, spacing)
            pts = sr._chaikin_smooth(pts, iterations=1)
            pts = sr._resample_stroke_points(pts, spacing)
            if len(pts) >= 2 and sr._stroke_cumulative_length(pts)[-1] > 2.0:
                out.append([(int(round(x)), int(round(y))) for x, y in pts])
        return sr._order_skeleton_strokes(out, direction=direction)

    # ── 落墨（限制在 allowed 内）──
    def _reveal_ink_segment(self, a: tuple[int, int], b: tuple[int, int], allowed: np.ndarray) -> None:
        seg = np.zeros((self.out_h, self.out_w), dtype=np.uint8)
        thick = max(1, self.cfg.ink_reveal_radius * 2 + 1)
        cv2.line(seg, a, b, 255, thickness=thick, lineType=cv2.LINE_AA)
        revealed = (seg > 0) & self.ink_pixels & allowed
        self.drawn[revealed] = self.ink_paint[revealed]

    def _ink_stamp_cell(self, cell: tuple[int, int], allowed: np.ndarray) -> None:
        r, c = cell
        e = self.cfg.grid_edge
        block = self.grid_blocks[r, c]
        allow_block = allowed[r * e:r * e + e, c * e:c * e + e]
        ink_region = (block < self.cfg.ink_threshold) & allow_block
        paint = np.repeat(block[:, :, None], 3, axis=2)
        target = self.drawn[r * e:r * e + e, c * e:c * e + e]
        target[ink_region] = paint[ink_region]

    def _color_stamp(self, px: int, py: int, disk: np.ndarray, allowed: np.ndarray) -> None:
        radius = self.cfg.brush_radius
        h, w = self.out_h, self.out_w
        y0, y1 = max(0, py - radius), min(h, py + radius + 1)
        x0, x1 = max(0, px - radius), min(w, px + radius + 1)
        if y1 <= y0 or x1 <= x0:
            return
        by0, by1 = y0 - (py - radius), disk.shape[0] - ((py + radius + 1) - y1)
        bx0, bx1 = x0 - (px - radius), disk.shape[1] - ((px + radius + 1) - x1)
        m = disk[by0:by1, bx0:bx1] * allowed[y0:y1, x0:x1]
        inv = 1.0 - m
        target = self.drawn[y0:y1, x0:x1]
        source = self.color_img[y0:y1, x0:x1].astype(np.float32)
        for ch in range(3):
            target[:, :, ch] = target[:, :, ch] * inv + source[:, :, ch] * m

    # ── 起笔段（骨架模式）：沿笔迹逐段揭原图墨迹，无块填充 ──
    def _lay_ink(self, writer, frames: int, samples: list[tuple[int, int]],
                 pen_lifts: set[int], allowed: np.ndarray) -> None:
        if frames <= 0:
            return
        n = len(samples)
        if n == 0:
            for _ in range(frames):
                writer.write(self._snapshot_with_tip(self.out_w // 2, self.out_h // 2))
            return
        idx_for_frame = _frame_progress_indices(n, frames)
        last: int | None = None
        for si in idx_for_frame:
            if last is None:
                self._reveal_ink_segment(samples[si], samples[si], allowed)
            else:
                for k in range(last + 1, si + 1):
                    if k in pen_lifts:
                        continue
                    self._reveal_ink_segment(samples[k - 1], samples[k], allowed)
            sx, sy = samples[si]
            writer.write(self._snapshot_with_tip(sx, sy))
            last = si

    # ── 添彩段：brush 或 contour-wipe，限制在 allowed 内 ──
    def _wash_brush(self, writer, frames: int, centers: list[tuple[int, int]], allowed: np.ndarray) -> None:
        if frames <= 0:
            return
        n = len(centers)
        if n == 0:
            for _ in range(frames):
                writer.write(self._snapshot_with_tip(self.out_w // 2, self.out_h // 2))
            return
        disk = sr._feathered_disk(self.cfg.brush_radius)
        idx_for_frame = _frame_progress_indices(n, frames)
        last: int | None = None
        for ci in idx_for_frame:
            if last is None:
                self._color_stamp(*centers[ci], disk, allowed)
            else:
                for k in range(last + 1, ci + 1):
                    self._color_stamp(*centers[k], disk, allowed)
            cx, cy = centers[ci]
            writer.write(self._snapshot_with_tip(cx, cy))
            last = ci

    def _wash_contour(self, writer, frames: int, allowed: np.ndarray) -> None:
        if frames <= 0:
            return
        cfg = self.cfg
        ys_all, xs_all = np.where(allowed)
        if ys_all.size == 0:
            return
        top, bottom = int(ys_all.min()), int(ys_all.max())
        left, right = int(xs_all.min()), int(xs_all.max())
        region_h = bottom - top + 1
        region_w = right - left + 1

        # 区域内的阻力场（墨线膨胀 + 模糊 + 逐行向下衰减）
        ink_u8 = ((self.ink_pixels & allowed)[top:bottom + 1, left:right + 1].astype(np.uint8)) * 255
        spread = int(np.clip(min(region_w, region_h) // 32, 3, 17))
        if spread % 2 == 0:
            spread = max(3, spread - 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (spread, spread))
        dilated = cv2.dilate(ink_u8, kernel, iterations=1)
        blur_r = max(1, int(round(min(region_w, region_h) / 220.0)))
        if blur_r % 2 == 0:
            blur_r += 1
        resistance = cv2.GaussianBlur(dilated, (blur_r, blur_r), 0).astype(np.float32)
        peak = float(resistance.max())
        resistance = resistance / peak if peak > 1e-6 else np.zeros_like(resistance)
        decay = cfg.wipe_decay
        for row in range(1, region_h):
            resistance[row] = np.maximum(resistance[row], resistance[row - 1] * decay)

        wave = sr._build_wipe_wave(region_w)
        delay_px = int(np.clip(region_h * cfg.wipe_delay_ratio, 12, 52))
        ys = np.arange(region_h, dtype=np.float32)[:, None]
        sweep = region_h + 2 * delay_px
        blocks = max(1, cfg.wipe_blocks)

        allowed_crop = allowed[top:bottom + 1, left:right + 1]
        color_crop = self.color_img[top:bottom + 1, left:right + 1].astype(np.float32)
        drawn_crop = self.drawn[top:bottom + 1, left:right + 1]

        for fi in range(frames):
            progress = 1.0 if frames == 1 else fi / (frames - 1)
            lead = sr._ease_in_out_sine(progress) * sweep - delay_px
            threshold = lead + wave[None, :] - resistance * delay_px
            reveal = (ys <= threshold) & allowed_crop
            drawn_crop[reveal] = color_crop[reveal]

            lane = sr._ease_in_out_sine((fi / blocks * 2.0) % 1.0)
            forward = (int(fi // blocks) % 2 == 0)
            cx = int(lane * region_w) if forward else int((1.0 - lane) * region_w)
            cx = max(0, min(region_w - 1, cx))
            col = np.where(reveal[:, cx])[0]
            cy = int(col[-1]) if col.size > 0 else 0
            writer.write(self._snapshot_with_tip(left + cx, top + cy))

        # 收尾：确保区域内允许像素全部揭示
        drawn_crop[allowed_crop] = color_crop[allowed_crop]

    # ── 网格路径的采样计划（插值 + 抬笔 + 块填充索引）──
    def _grid_plan(self, path: list[tuple[int, int]]):
        samples: list[tuple[int, int]] = []
        pen_lifts: set[int] = set()
        sample_cell: list[int] = []
        for idx, cell in enumerate(path):
            cx, cy = self._cell_center(cell)
            if idx == 0:
                samples.append((cx, cy))
                sample_cell.append(idx)
                continue
            prev_cell = path[idx - 1]
            prev = self._cell_center(prev_cell)
            if math.hypot(cell[0] - prev_cell[0], cell[1] - prev_cell[1]) > math.sqrt(2):
                pen_lifts.add(len(samples))
                samples.append((cx, cy))
                sample_cell.append(idx)
                continue
            steps = max(1, int(math.hypot(cx - prev[0], cy - prev[1]) / self.cfg.sample_step))
            for s in range(1, steps + 1):
                samples.append((int(prev[0] + (cx - prev[0]) * s / steps),
                                int(prev[1] + (cy - prev[1]) * s / steps)))
                sample_cell.append(idx)
        return samples, pen_lifts, sample_cell

    # ── 主渲染 ──
    def render_to(
        self,
        output_path: Path,
        total_ms: int,
        *,
        target_frame_count: int | None = None,
        scene_start_ms: int = 0,
        scene_start_frame: int = 0,
        sink_factory=None,
    ) -> Path:
        cfg = self.cfg
        elements = sorted(self.ann["elements"], key=lambda e: e["reveal"]["startMs"])
        weight_sum = cfg.ink_weight + cfg.color_weight
        formal_clock = target_frame_count is not None
        if target_frame_count is None:
            target_frame_count = round(total_ms * cfg.fps / 1000)
        if target_frame_count <= 0:
            raise RuntimeError("目标帧数必须为正整数")
        factory = sink_factory or ffmpeg_frame_sink.FFmpegFrameSink
        writer = factory(
            output_path,
            width=self.out_w,
            height=self.out_h,
            fps=cfg.fps,
            expected_frame_count=target_frame_count,
        )
        cur_frame = 0

        def boundary(local_ms: int) -> int:
            if formal_clock:
                return render_timing.local_frame_boundary(
                    local_ms,
                    scene_start_ms=scene_start_ms,
                    scene_start_frame=scene_start_frame,
                    fps=cfg.fps,
                )
            return round(local_ms * cfg.fps / 1000)

        def fill_static(until_frame: int) -> None:
            nonlocal cur_frame
            if until_frame < cur_frame:
                raise RuntimeError("标注帧边界倒退或元素发生重叠")
            n = until_frame - cur_frame
            if n == 0:
                return
            snap = self.drawn.astype(np.uint8)
            for _ in range(n):
                writer.write(snap)
            cur_frame += n

        try:
            for idx, element in enumerate(elements):
                reveal = element["reveal"]
                direction = reveal.get("direction", "left-to-right")
                start_ms = reveal["startMs"]
                dur_ms = reveal["durationMs"]
                start_frame = boundary(start_ms)
                end_frame = boundary(start_ms + dur_ms)
                # 正式质量合同要求第 0 帧是纯净纸底。即使首个元素从
                # 0ms 开始，也先发布一帧未落墨、未叠加手部的画布；
                # 后续帧仍在原有 end_frame 内完成该元素，不改变总帧数。
                if idx == 0 and cur_frame == 0 and start_frame == 0 and end_frame - start_frame >= 2:
                    writer.write(self.drawn.astype(np.uint8))
                    cur_frame = 1
                    start_frame = 1
                fill_static(start_frame)

                allowed = self._allowed_mask(element, elements[idx + 1:])
                element_frames = end_frame - start_frame
                if element_frames <= 0:
                    raise RuntimeError("元素时长不足一个权威视频帧")
                if element_frames == 1:
                    ink_frames, color_frames = 1, 0
                else:
                    ink_frames = max(1, round(element_frames * cfg.ink_weight / weight_sum))
                    color_frames = element_frames - ink_frames

                if cfg.ink_path_mode == "skeleton":
                    strokes = self._region_skeleton_strokes(allowed, direction)
                    if strokes:
                        samples, pen_lifts = [], set()
                        for si, stroke in enumerate(strokes):
                            if si > 0:
                                pen_lifts.add(len(samples))
                            samples.extend(stroke)
                        self._lay_ink(writer, ink_frames, samples, pen_lifts, allowed)
                        centers = samples
                    else:
                        path = self._region_grid_path(allowed, direction)
                        samples, pen_lifts, _ = self._grid_plan(path) if path else ([], set(), [])
                        self._lay_ink(writer, ink_frames, samples, pen_lifts, allowed)
                        centers = [self._cell_center(c) for c in path]
                else:
                    path = self._region_grid_path(allowed, direction)
                    if path:
                        samples, pen_lifts, sample_cell = self._grid_plan(path)
                        # 块填充：随笔尖推进逐格铺满（保证文字/大块实心）
                        self._lay_ink_grid(writer, ink_frames, samples, pen_lifts, sample_cell, path, allowed)
                        centers = [self._cell_center(c) for c in path]
                    else:
                        self._lay_ink(writer, ink_frames, [], set(), allowed)
                        centers = []

                cur_frame += ink_frames

                if cfg.color_fill == "contour-wipe":
                    self._wash_contour(writer, color_frames, allowed)
                else:
                    self._wash_brush(writer, color_frames, centers, allowed)
                cur_frame += color_frames

            # 凝视严格只占权威剩余帧；标注层必须预留至少 0.5 秒。
            self.drawn[...] = self.color_img.astype(np.float32)
            fill_static(target_frame_count)
            if cur_frame != target_frame_count:
                raise RuntimeError("实际写入帧数与权威 frameCount 不一致")
        except Exception:
            writer.abort()
            raise
        else:
            writer.close()
        return output_path

    # 网格起笔专用：带块填充，笔尖与揭墨同步
    def _lay_ink_grid(self, writer, frames: int, samples, pen_lifts, sample_cell, path, allowed) -> None:
        if frames <= 0:
            return
        n = len(samples)
        if n == 0:
            for _ in range(frames):
                writer.write(self._snapshot_with_tip(self.out_w // 2, self.out_h // 2))
            return
        idx_for_frame = _frame_progress_indices(n, frames)
        cells_done = 0
        last: int | None = None
        for si in idx_for_frame:
            if last is None:
                self._reveal_ink_segment(samples[si], samples[si], allowed)
            else:
                for k in range(last + 1, si + 1):
                    if k in pen_lifts:
                        continue
                    self._reveal_ink_segment(samples[k - 1], samples[k], allowed)
            target_cell = sample_cell[si]
            while cells_done <= target_cell and cells_done < len(path):
                self._ink_stamp_cell(path[cells_done], allowed)
                cells_done += 1
            sx, sy = samples[si]
            writer.write(self._snapshot_with_tip(sx, sy))
            last = si
        while cells_done < len(path):
            self._ink_stamp_cell(path[cells_done], allowed)
            cells_done += 1


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="SRT 白板动画整合渲染器（mask 编排 + stream 画法）")
    p.add_argument("image", nargs="?", help="standalone 线稿图路径")
    p.add_argument("annotation", nargs="?", help="standalone 同名 annotation.json 路径")
    p.add_argument("output", nargs="?", help="standalone 输出 MP4 路径")
    p.add_argument("hand", nargs="?", default=str(DEFAULT_HAND), help="手部素材 PNG（默认内置）")
    p.add_argument("--project", help="正式项目根目录；与 --scene-id 一起使用")
    scene_selector = p.add_mutually_exclusive_group()
    scene_selector.add_argument("--scene-id", help="正式项目中的单个 sceneId")
    scene_selector.add_argument("--all", dest="all_scenes", action="store_true",
                                help="按 generation plan 顺序批量渲染全部正式场景")
    scene_selector.add_argument("--scene-ids", nargs="+",
                                help="批量渲染指定 sceneId；实际执行仍按 generation plan 排序")
    p.add_argument("--allow-v1-disabled-compat", action="store_true",
                   help="显式允许 schema v1 Disabled 只读兼容视图")
    p.add_argument("--total-ms", type=int, default=None, help="总时长；缺省用标注 sceneDurationMs")
    p.add_argument("--bare-tip", action="store_true", help="不叠加笔尖/手部")
    p.add_argument("--ink-path", default="grid", choices=["grid", "skeleton"],
                   help="笔迹路径: grid 网格(默认); skeleton 骨架追踪")
    p.add_argument("--color-fill", default="contour-wipe", choices=["contour-wipe", "brush"],
                   help="上色: contour-wipe 轮廓扫描(默认); brush 沿轨迹刷")
    p.add_argument("--pause", default="heavy", choices=["heavy", "auto", "light", "off"],
                   help="起笔段停顿节奏（预留，逐区域画法下影响较弱）")
    p.add_argument("--fps", type=int, default=None)
    p.add_argument("--grid-edge", type=int, default=None)
    p.add_argument("--brush-radius", type=int, default=None)
    p.add_argument("--cap-long-edge", type=int, default=None,
                   help="输出长边像素上限（预览可调小加速，默认 1080）")
    return p.parse_args(argv)


def _build_cfg(args) -> sr.Config:
    kw: dict = {}
    if args.fps is not None:
        kw["fps"] = args.fps
    if args.grid_edge is not None:
        kw["grid_edge"] = args.grid_edge
    if args.brush_radius is not None:
        kw["brush_radius"] = args.brush_radius
    if args.cap_long_edge is not None:
        kw["cap_long_edge"] = args.cap_long_edge
    kw["ink_path_mode"] = args.ink_path
    kw["color_fill"] = args.color_fill
    kw["pause_mode"] = args.pause
    return sr.Config(**kw)


def _formal_contexts(args):
    if not args.project or not (args.scene_id or args.all_scenes or args.scene_ids):
        raise render_timing.RenderTimingError(
            "正式渲染必须提供 --project，并选择 --scene-id、--all 或 --scene-ids"
        )
    if any(value is not None for value in (args.image, args.annotation, args.output)):
        raise render_timing.RenderTimingError("正式渲染不得用位置参数覆盖项目图片、annotation 或输出路径")
    if args.total_ms is not None or args.fps is not None or args.cap_long_edge is not None:
        raise render_timing.RenderTimingError("正式渲染不得用未持久化的 total-ms/fps/尺寸覆盖项目合同")
    project = project_workspace.load_project(args.project)
    plan_ids = [item["sceneId"] for item in project.plan["scenes"]]
    if args.all_scenes:
        requested_ids = plan_ids
    elif args.scene_ids:
        if len(set(args.scene_ids)) != len(args.scene_ids):
            raise render_timing.RenderTimingError("--scene-ids 不得重复")
        unknown = set(args.scene_ids) - set(plan_ids)
        if unknown:
            raise render_timing.RenderTimingError("--scene-ids 包含未知 sceneId")
        requested = set(args.scene_ids)
        requested_ids = [scene_id for scene_id in plan_ids if scene_id in requested]
    else:
        if args.scene_id not in set(plan_ids):
            raise render_timing.RenderTimingError("--scene-id 不属于 current generation plan")
        requested_ids = [args.scene_id]

    frozen = render_timing.build_formal_validation_context(project)
    all_formals = render_timing.resolve_formal_scenes(
        project,
        plan_ids,
        context=frozen,
        allow_v1_disabled_compat=args.allow_v1_disabled_compat,
    )
    annotation_review.require_current_annotation_review_approval(
        project,
        context=frozen,
        formals=all_formals,
    )
    by_id = {formal.scene_id: formal for formal in all_formals}
    selected = tuple(by_id[scene_id] for scene_id in requested_ids)
    render_timing.validate_formal_context_current(project, frozen)
    return frozen, selected


def _formal_context(args):
    _frozen, contexts = _formal_contexts(args)
    if len(contexts) != 1:
        raise render_timing.RenderTimingError("单幕兼容入口只接受一个 sceneId")
    return contexts[0]


def _deep_receipt(media: dict) -> dict:
    validation = media.get("validation")
    receipt = validation.get("deepReceipt") if isinstance(validation, dict) else None
    if not isinstance(receipt, dict):
        raise media_validation.MediaValidationError("候选媒体缺少 current deep receipt")
    return receipt


def _elapsed_ms(started_ns: int) -> float:
    """返回适合运行审计的单调时钟毫秒值，不参与任何作品 identity。"""

    return round((time.perf_counter_ns() - started_ns) / 1_000_000, 3)


def _runtime_metrics_summary(
    scene_metrics: list[dict],
    *,
    batch_started_ns: int,
    context_ms: float,
    prepare_ms: float,
    candidate_execution_ms: float,
    coordinator_publish_ms: float,
) -> dict:
    """汇总正式 batch 的本机运行指标；这些字段只用于审计与 benchmark。"""

    candidate_bytes_by_scene = {
        item["sceneId"]: item["candidateBytes"]
        for item in scene_metrics
        if isinstance(item.get("sceneId"), str)
        and isinstance(item.get("candidateBytes"), int)
        and item["candidateBytes"] > 0
    }
    worker_phase_durations_ms = {
        phase: round(
            sum(
                item.get("phaseDurationsMs", {}).get(phase, 0.0)
                for item in scene_metrics
                if isinstance(item.get("phaseDurationsMs"), dict)
                and isinstance(item["phaseDurationsMs"].get(phase, 0.0), (int, float))
            ),
            3,
        )
        for phase in ("prepare", "render", "validation")
    }
    return {
        "wallMs": _elapsed_ms(batch_started_ns),
        "stageDurationsMs": {
            "context": context_ms,
            "prepare": prepare_ms,
            "candidateExecution": candidate_execution_ms,
            "coordinatorPublish": coordinator_publish_ms,
        },
        # 每个成功创建的 FFmpegFrameSink 对应一个受控 scene encoder 子进程。
        # 这是整批累计启动数/编码负载代理，不是 OS 级同时在途峰值。
        "ffmpegProcessCount": sum(
            item.get("ffmpegProcessCount", 0)
            for item in scene_metrics
            if isinstance(item.get("ffmpegProcessCount", 0), int)
        ),
        "ffmpegProcessCountMetric": "sceneEncoderStarts",
        # worker 阶段为逐幕耗时之和，并发时可能大于 wallMs。
        "workerPhaseDurationsMs": worker_phase_durations_ms,
        "workerPhaseDurationAggregation": "sumAcrossScenes",
        # 只累计通过 deep validation 的 candidate；失败 candidate 不冒充可用产物。
        "candidateBytes": sum(candidate_bytes_by_scene.values()),
        "candidateBytesByScene": candidate_bytes_by_scene,
    }


def _publish_and_bind_scene(
    candidate: Path,
    destination: Path,
    *,
    render_profile: dict,
    expected_frame_count: int,
    deep_receipt: dict,
) -> dict:
    """发布已 deep 的候选；binding 失败时恢复发布前的正式文件。"""

    backup = candidate.with_name(f"{candidate.name}.previous-formal")
    had_formal = destination.is_file()
    if destination.exists() and not had_formal:
        raise media_validation.MediaValidationError("正式 scene 目标不是普通文件")
    if had_formal:
        try:
            os.link(destination, backup)
        except OSError:
            shutil.copy2(destination, backup)
    try:
        media_validation.atomic_publish(candidate, destination)
        try:
            return media_validation.bind_validated_video(
                destination,
                render_profile=render_profile,
                expected_frame_count=expected_frame_count,
                expected_audio_streams=0,
                deep_receipt=deep_receipt,
            )
        except Exception:
            try:
                if destination.is_file():
                    os.replace(destination, candidate)
                if had_formal and backup.is_file():
                    os.replace(backup, destination)
            except OSError as restore_error:
                raise media_validation.MediaValidationError(
                    "scene 发布后 binding 失败，旧正式文件恢复失败"
                ) from restore_error
            raise
    finally:
        backup.unlink(missing_ok=True)


def validate_scene_media_batch(
    project: project_workspace.Project,
    scene_ids: list[str] | tuple[str, ...] | None = None,
    *,
    workspace_config: project_workspace.WorkspaceConfig | None = None,
):
    """并发复核彼此独立的正式 scene，协调器按 generation plan 顺序提交。"""

    requested = (
        {item["sceneId"] for item in project.plan["scenes"]}
        if scene_ids is None
        else set(scene_ids)
    )
    known = {item["sceneId"] for item in project.plan["scenes"]}
    unknown = requested - known
    if unknown:
        raise render_timing.RenderTimingError("scene media validation 包含未知 sceneId")
    ordered_ids = [
        item["sceneId"] for item in project.plan["scenes"] if item["sceneId"] in requested
    ]
    contexts = render_timing.resolve_formal_scenes(project, ordered_ids)
    manifest_path = project.path(render_timing.RENDER_MANIFEST_FILE)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise render_timing.RenderTimingError("无法读取 current render manifest") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 1
        or manifest.get("projectId") != project.project_id
        or not isinstance(manifest.get("scenes"), dict)
    ):
        raise render_timing.RenderTimingError("render manifest 与 current project 不一致")

    tasks: list[dict] = []
    for context in contexts:
        manifest_scene = manifest["scenes"].get(context.scene_id)
        if not isinstance(manifest_scene, dict):
            raise render_timing.RenderTimingError("render manifest 缺少 current scene")
        if manifest_scene.get("outputFile") != context.output_path.relative_to(project.root).as_posix():
            raise render_timing.RenderTimingError("render manifest scene outputFile stale")
        render_options = manifest_scene.get("renderOptions")
        if (
            not isinstance(render_options, dict)
            or manifest_scene.get("renderIdentityHash")
            != render_timing.render_identity(context, render_options=render_options)
        ):
            raise render_timing.RenderTimingError("render manifest scene identity stale")
        previous_media = manifest_scene.get("media")
        receipt = None
        if isinstance(previous_media, dict):
            validation = previous_media.get("validation")
            if isinstance(validation, dict):
                receipt = validation.get("deepReceipt")
        tasks.append(
            {
                "sceneId": context.scene_id,
                "path": context.output_path,
                "frameCount": context.timing_scene["frameCount"],
                "receipt": receipt,
            }
        )

    config = workspace_config or project_workspace.load_workspace_config(verify_writable=False)
    workers = config.for_stage("sceneMediaValidation")

    def worker(task: dict):
        receipt = task["receipt"]
        if receipt is None:
            media = media_validation.validate_video(
                task["path"],
                render_profile=project.render_profile,
                expected_frame_count=task["frameCount"],
                expected_audio_streams=0,
            )
        else:
            media = media_validation.bind_validated_video(
                task["path"],
                render_profile=project.render_profile,
                expected_frame_count=task["frameCount"],
                expected_audio_streams=0,
                deep_receipt=receipt,
            )
        return bounded_execution.WorkerOutcome.success(
            {"sceneId": task["sceneId"], "media": media}
        )

    report = bounded_execution.execute_bounded(
        tasks,
        worker,
        max_workers=workers,
        failure_policy=bounded_execution.CONTINUE_INDEPENDENT,
    )
    changed = False
    for result in report.results:
        if result.status != "succeeded" or result.outcome is None:
            continue
        value = result.outcome.value
        if not isinstance(value, dict):
            continue
        manifest["scenes"][value["sceneId"]]["media"] = value["media"]
        changed = True
    if changed:
        project_workspace.write_json_atomic(manifest_path, manifest)
    return report


def _load_formal_hand(
    args,
    cfg: sr.Config,
) -> tuple[Path | None, tuple[np.ndarray, np.ndarray] | None, str | None]:
    hand_png = Path(args.hand) if args.hand else None
    if args.bare_tip or hand_png is None:
        return hand_png, None, None
    hand_data = sr._load_hand(hand_png, cfg.target_hand_height)
    hand_sha256 = (
        project_workspace.sha256_file(hand_png) if hand_png.is_file() else None
    )
    return hand_png, hand_data, hand_sha256


def _formal_render_options(
    args,
    cfg: sr.Config,
    *,
    hand_sha256: str | None,
) -> dict:
    return {
        "inkPath": cfg.ink_path_mode,
        "colorFill": cfg.color_fill,
        "pause": cfg.pause_mode,
        "gridEdge": cfg.grid_edge,
        "brushRadius": cfg.brush_radius,
        "bareTip": bool(args.bare_tip),
        "cleanFirstFrame": "when-first-element-has-at-least-2-frames",
        "paperBackground": "paper-content-mask-v3",
        "inkOrderingContract": "annotation-directional-bands-v1",
        "canvasHex": cfg.canvas_hex,
        "paperMask": {
            "grayCut": cfg.paper_gray_cut,
            "saturationCut": cfg.paper_saturation_cut,
            "diffCut": cfg.paper_diff_cut,
            "minComponentArea": cfg.paper_min_component_area,
            "edgeBandRatio": cfg.paper_edge_band_ratio,
            "edgeGrayCut": cfg.paper_edge_gray_cut,
        } if cfg.match_bg else None,
        "handSha256": hand_sha256,
    }


def _render_formal_candidate_worker(task: dict) -> dict:
    """只生成并 deep 验证 candidate；不得写正式 scene 或共享 manifest。"""

    started_ns = time.time_ns()
    result = {
        "sceneId": task["sceneId"],
        "candidatePath": task["candidatePath"],
        "pid": os.getpid(),
        "startedNs": started_ns,
    }
    ffmpeg_process_count = 0
    prepare_ms = 0.0
    render_ms = 0.0
    validation_ms = 0.0
    prepare_started_ns = time.perf_counter_ns()
    try:
        image_path = Path(task["imagePath"])
        annotation_path = Path(task["annotationPath"])
        candidate = Path(task["candidatePath"])
        hand_path = Path(task["handPath"]) if task.get("handPath") else None
        profile = task["renderProfile"]
        scene = task["timingScene"]
        cfg = sr.Config(**task["config"])

        if project_workspace.sha256_file(image_path) != task["imageSha256"]:
            raise render_timing.RenderTimingError(
                f"batch 期间 {task['sceneId']} current image 已变化"
            )
        if project_workspace.sha256_file(annotation_path) != task["annotationSha256"]:
            raise render_timing.RenderTimingError(
                f"batch 期间 {task['sceneId']} current annotation 已变化"
            )

        hand_data = None
        if not task["bareTip"] and hand_path is not None:
            if project_workspace.sha256_file(hand_path) != task.get("handSha256"):
                raise render_timing.RenderTimingError("batch 期间 current hand 素材已变化")
            hand_data = sr._load_hand(hand_path, cfg.target_hand_height)

        image_bgr = sr._imread_any(image_path)
        if image_bgr is None:
            raise render_timing.RenderTimingError(f"无法读取项目图片: {image_path}")
        actual_h, actual_w = image_bgr.shape[:2]
        if (actual_w, actual_h) != (profile["width"], profile["height"]):
            raise render_timing.RenderTimingError(
                f"项目图片尺寸必须为 {profile['width']}x{profile['height']}，"
                f"实际为 {actual_w}x{actual_h}"
            )
        renderer = RegionStreamRenderer(
            image_bgr,
            task["annotation"],
            cfg,
            hand_path,
            task["bareTip"],
            output_size=(profile["width"], profile["height"]),
            preloaded_hand=hand_data,
        )

        prepare_ms = _elapsed_ms(prepare_started_ns)

        def observed_sink_factory(*sink_args, **sink_kwargs):
            nonlocal ffmpeg_process_count
            sink = ffmpeg_frame_sink.FFmpegFrameSink(*sink_args, **sink_kwargs)
            ffmpeg_process_count += 1
            return sink

        render_started_ns = time.perf_counter_ns()
        renderer.render_to(
            candidate,
            scene["sceneDurationMs"],
            target_frame_count=scene["frameCount"],
            scene_start_ms=scene["startMs"],
            scene_start_frame=scene["startFrame"],
            sink_factory=observed_sink_factory,
        )
        render_ms = _elapsed_ms(render_started_ns)

        if project_workspace.sha256_file(image_path) != task["imageSha256"]:
            raise render_timing.RenderTimingError(
                f"batch 期间 {task['sceneId']} current image 已变化"
            )
        if project_workspace.sha256_file(annotation_path) != task["annotationSha256"]:
            raise render_timing.RenderTimingError(
                f"batch 期间 {task['sceneId']} current annotation 已变化"
            )
        if (
            not task["bareTip"]
            and hand_path is not None
            and project_workspace.sha256_file(hand_path) != task.get("handSha256")
        ):
            raise render_timing.RenderTimingError("batch 期间 current hand 素材已变化")

        validation_started_ns = time.perf_counter_ns()
        candidate_media = media_validation.validate_video(
            candidate,
            render_profile=profile,
            expected_frame_count=scene["frameCount"],
            expected_audio_streams=0,
        )
        validation_ms = _elapsed_ms(validation_started_ns)
        result.update(
            {
                "status": "succeeded",
                "deepReceipt": _deep_receipt(candidate_media),
                "candidateBytes": candidate.stat().st_size,
            }
        )
    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "stage": "candidate_generation_deep_validation",
                "errorType": type(exc).__name__,
                "error": str(exc),
                "exitCode": _formal_error_exit_code(exc),
            }
        )
    result.update(
        {
            "phaseDurationsMs": {
                "prepare": prepare_ms,
                "render": render_ms,
                "validation": validation_ms,
            },
            "ffmpegProcessCount": ffmpeg_process_count,
            "finishedNs": time.time_ns(),
        }
    )
    return result


def _peak_worker_overlap(results: list[dict]) -> int:
    events: list[tuple[int, int]] = []
    for result in results:
        started = result.get("startedNs")
        finished = result.get("finishedNs")
        if isinstance(started, int) and isinstance(finished, int) and finished >= started:
            events.append((started, 1))
            events.append((finished, -1))
    active = 0
    peak = 0
    # 结束与下一幕启动恰好同一纳秒时，先收尾再计入下一幕，避免虚增并发峰值。
    for _timestamp, delta in sorted(events, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return max(peak, 1 if events else 0)


def _execute_formal_candidate_tasks(
    tasks: list[dict],
    max_workers: int,
    *,
    worker=None,
    executor_factory=None,
) -> tuple[dict[str, dict], int]:
    """有界执行独立场景任务；返回值不携带任何正式写入。"""

    task_worker = worker or _render_formal_candidate_worker
    if not tasks:
        return {}, 0
    if max_workers <= 1:
        ordered = [task_worker(task) for task in tasks]
        return {item["sceneId"]: item for item in ordered}, _peak_worker_overlap(ordered)

    completed: list[dict] = []
    if executor_factory is None:
        spawn_context = multiprocessing.get_context("spawn")
        executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=spawn_context,
        )
    else:
        executor = executor_factory(max_workers=max_workers)
    with executor:
        future_tasks = {
            executor.submit(task_worker, task): task
            for task in tasks
        }
        for future in concurrent.futures.as_completed(future_tasks):
            task = future_tasks[future]
            try:
                completed.append(future.result())
            except Exception as exc:
                completed.append(
                    {
                        "sceneId": task["sceneId"],
                        "candidatePath": task["candidatePath"],
                        "status": "failed",
                        "stage": "worker_process",
                        "errorType": type(exc).__name__,
                        "error": str(exc),
                        "exitCode": 4,
                    }
                )
    return {item["sceneId"]: item for item in completed}, _peak_worker_overlap(completed)


def _render_formal_context(
    args,
    context: render_timing.FormalSceneRender,
    frozen: render_timing.FormalValidationContext,
    cfg: sr.Config,
    *,
    hand_png: Path | None,
    preloaded_hand: tuple[np.ndarray, np.ndarray] | None,
    hand_sha256: str | None,
    runtime_metrics: dict | None = None,
) -> tuple[Path, str]:
    prepare_started_ns = time.perf_counter_ns()
    profile = context.project.render_profile
    render_timing.validate_formal_context_current(context.project, frozen)
    image_sha256 = project_workspace.sha256_file(context.image_path)
    annotation_sha256 = project_workspace.sha256_file(context.annotation_path)
    image_bgr = sr._imread_any(context.image_path)
    if image_bgr is None:
        raise render_timing.RenderTimingError(f"无法读取项目图片: {context.image_path}")
    actual_h, actual_w = image_bgr.shape[:2]
    if (actual_w, actual_h) != (profile["width"], profile["height"]):
        raise render_timing.RenderTimingError(
            f"项目图片尺寸必须为 {profile['width']}x{profile['height']}，实际为 {actual_w}x{actual_h}"
        )
    renderer = RegionStreamRenderer(
        image_bgr,
        context.annotation,
        cfg,
        hand_png,
        args.bare_tip,
        output_size=(profile["width"], profile["height"]),
        preloaded_hand=preloaded_hand,
    )
    run_dir = context.project.create_run_dir(f"render-scene-{uuid.uuid4().hex}")
    candidate = run_dir / "scene.h264.candidate.mp4"
    scene = context.timing_scene
    ffmpeg_process_count = 0
    prepare_ms = _elapsed_ms(prepare_started_ns)

    def observed_sink_factory(*sink_args, **sink_kwargs):
        nonlocal ffmpeg_process_count
        sink = ffmpeg_frame_sink.FFmpegFrameSink(*sink_args, **sink_kwargs)
        ffmpeg_process_count += 1
        return sink

    render_started_ns = time.perf_counter_ns()
    renderer.render_to(
        candidate,
        scene["sceneDurationMs"],
        target_frame_count=scene["frameCount"],
        scene_start_ms=scene["startMs"],
        scene_start_frame=scene["startFrame"],
        sink_factory=observed_sink_factory,
    )
    render_ms = _elapsed_ms(render_started_ns)
    render_timing.validate_formal_context_current(context.project, frozen)
    if project_workspace.sha256_file(context.image_path) != image_sha256:
        raise render_timing.RenderTimingError(
            f"batch 期间 {context.scene_id} current image 已变化"
        )
    if project_workspace.sha256_file(context.annotation_path) != annotation_sha256:
        raise render_timing.RenderTimingError(
            f"batch 期间 {context.scene_id} current annotation 已变化"
        )
    validation_started_ns = time.perf_counter_ns()
    candidate_media = media_validation.validate_video(
        candidate,
        render_profile=profile,
        expected_frame_count=scene["frameCount"],
        expected_audio_streams=0,
    )
    validation_ms = _elapsed_ms(validation_started_ns)
    candidate_bytes = candidate.stat().st_size
    publish_started_ns = time.perf_counter_ns()
    media = _publish_and_bind_scene(
        candidate,
        context.output_path,
        render_profile=profile,
        expected_frame_count=scene["frameCount"],
        deep_receipt=_deep_receipt(candidate_media),
    )
    render_options = _formal_render_options(args, cfg, hand_sha256=hand_sha256)
    manifest = render_timing.update_render_manifest(
        context,
        media=media,
        render_options=render_options,
    )
    try:
        run_dir.rmdir()
    except OSError:
        pass
    identity = manifest["scenes"][context.scene_id]["renderIdentityHash"]
    if runtime_metrics is not None:
        runtime_metrics.update(
            {
                "sceneId": context.scene_id,
                "phaseDurationsMs": {
                    "prepare": prepare_ms,
                    "render": render_ms,
                    "validation": validation_ms,
                },
                "coordinatorPublishMs": _elapsed_ms(publish_started_ns),
                "ffmpegProcessCount": ffmpeg_process_count,
                "candidateBytes": candidate_bytes,
            }
        )
    return context.output_path, identity


def _formal_cfg(args, profile: dict) -> sr.Config:
    cfg_args = argparse.Namespace(**vars(args))
    cfg_args.fps = profile["fps"]
    cfg_args.cap_long_edge = profile["width"]
    return _build_cfg(cfg_args)


def _run_formal(args) -> tuple[Path, str]:
    frozen, contexts = _formal_contexts(args)
    if len(contexts) != 1:
        raise render_timing.RenderTimingError("单幕兼容入口只接受一个 sceneId")
    context = contexts[0]
    cfg = _formal_cfg(args, context.project.render_profile)
    hand_png, hand_data, hand_sha256 = _load_formal_hand(args, cfg)
    return _render_formal_context(
        args,
        context,
        frozen,
        cfg,
        hand_png=hand_png,
        preloaded_hand=hand_data,
        hand_sha256=hand_sha256,
    )


def _run_formal_batch(args) -> dict:
    batch_started_ns = time.perf_counter_ns()
    context_started_ns = time.perf_counter_ns()
    frozen, contexts = _formal_contexts(args)
    context_ms = _elapsed_ms(context_started_ns)
    if not contexts:
        raise render_timing.RenderTimingError("正式 batch 没有可渲染场景")
    prepare_started_ns = time.perf_counter_ns()
    project = contexts[0].project
    configured = project_workspace.load_workspace_config(
        verify_writable=False
    ).for_stage("sceneRender")
    cfg = _formal_cfg(args, project.render_profile)
    hand_png, hand_data, hand_sha256 = _load_formal_hand(args, cfg)
    effective = min(configured, len(contexts))
    if effective == 1:
        results: list[dict] = []
        scene_metrics: list[dict] = []
        prepare_ms = _elapsed_ms(prepare_started_ns)
        candidate_execution_ms = 0.0
        coordinator_publish_ms = 0.0
        for context in contexts:
            runtime_metrics: dict = {}
            scene_started_ns = time.perf_counter_ns()
            output, identity = _render_formal_context(
                args,
                context,
                frozen,
                cfg,
                hand_png=hand_png,
                preloaded_hand=hand_data,
                hand_sha256=hand_sha256,
                runtime_metrics=runtime_metrics,
            )
            scene_wall_ms = _elapsed_ms(scene_started_ns)
            publish_ms = runtime_metrics.get("coordinatorPublishMs", 0.0)
            coordinator_publish_ms += publish_ms
            candidate_execution_ms += max(0.0, scene_wall_ms - publish_ms)
            scene_metrics.append(runtime_metrics)
            results.append(
                {
                    "sceneId": context.scene_id,
                    "outputFile": output.relative_to(project.root).as_posix(),
                    "renderIdentityHash": identity,
                    "status": "published_current_technical",
                }
            )
        summary = {
            "contractVersion": "whiteboard-scene-render-batch-v2",
            "status": "PASS",
            "partialSuccess": False,
            "projectId": project.project_id,
            "taskCount": len(contexts),
            "configured": configured,
            "effective": 1,
            "peak": 1,
            "configuredSceneRenderConcurrency": configured,
            "effectiveSceneRenderConcurrency": 1,
            "peakSceneRenderWorkers": 1,
            "sceneOrder": [context.scene_id for context in contexts],
            "results": results,
            "successCount": len(results),
            "failureCount": 0,
            "approvalWritten": False,
            "userConfirmationRequired": True,
        }
        summary.update(
            _runtime_metrics_summary(
                scene_metrics,
                batch_started_ns=batch_started_ns,
                context_ms=context_ms,
                prepare_ms=prepare_ms,
                candidate_execution_ms=round(candidate_execution_ms, 3),
                coordinator_publish_ms=round(coordinator_publish_ms, 3),
            )
        )
        return summary

    render_options = _formal_render_options(args, cfg, hand_sha256=hand_sha256)
    tasks: list[dict] = []
    expected_by_scene: dict[str, dict] = {}
    for context in contexts:
        image_sha256 = project_workspace.sha256_file(context.image_path)
        annotation_sha256 = project_workspace.sha256_file(context.annotation_path)
        run_dir = context.project.create_run_dir(f"render-scene-{uuid.uuid4().hex}")
        candidate = run_dir / "scene.h264.candidate.mp4"
        expected_by_scene[context.scene_id] = {
            "imageSha256": image_sha256,
            "annotationSha256": annotation_sha256,
            "runDir": run_dir,
        }
        tasks.append(
            {
                "sceneId": context.scene_id,
                "imagePath": str(context.image_path),
                "annotationPath": str(context.annotation_path),
                "candidatePath": str(candidate),
                "annotation": context.annotation,
                "timingScene": context.timing_scene,
                "renderProfile": project.render_profile,
                "config": vars(cfg).copy(),
                "handPath": str(hand_png) if hand_png is not None else None,
                "handSha256": hand_sha256,
                "bareTip": bool(args.bare_tip),
                "imageSha256": image_sha256,
                "annotationSha256": annotation_sha256,
            }
        )

    prepare_ms = _elapsed_ms(prepare_started_ns)
    candidate_execution_started_ns = time.perf_counter_ns()
    worker_results, peak = _execute_formal_candidate_tasks(
        tasks,
        max_workers=effective,
    )
    candidate_execution_ms = _elapsed_ms(candidate_execution_started_ns)
    scene_metrics = [
        {
            "sceneId": context.scene_id,
            "phaseDurationsMs": worker_results.get(context.scene_id, {}).get(
                "phaseDurationsMs",
                {"prepare": 0.0, "render": 0.0, "validation": 0.0},
            ),
            "ffmpegProcessCount": worker_results.get(context.scene_id, {}).get(
                "ffmpegProcessCount", 0
            ),
            "candidateBytes": worker_results.get(context.scene_id, {}).get(
                "candidateBytes", 0
            ),
        }
        for context in contexts
    ]
    results: list[dict] = []
    coordinator_publish_started_ns = time.perf_counter_ns()
    for context in contexts:
        worker_result = worker_results.get(context.scene_id)
        if not isinstance(worker_result, dict) or worker_result.get("status") != "succeeded":
            failure = worker_result or {
                "stage": "worker_process",
                "errorType": "MissingWorkerResult",
                "error": "worker 未返回场景结果",
                "exitCode": 4,
            }
            results.append(
                {
                    "sceneId": context.scene_id,
                    "outputFile": context.output_path.relative_to(project.root).as_posix(),
                    "status": "failed",
                    "stage": failure.get("stage", "candidate_generation_deep_validation"),
                    "errorType": failure.get("errorType", "RenderError"),
                    "error": failure.get("error", "candidate 生成或 deep validation 失败"),
                    "exitCode": failure.get("exitCode", 4),
                }
            )
            continue

        expected = expected_by_scene[context.scene_id]
        try:
            render_timing.validate_formal_context_current(project, frozen)
            if project_workspace.sha256_file(context.image_path) != expected["imageSha256"]:
                raise render_timing.RenderTimingError(
                    f"batch 发布前 {context.scene_id} current image 已变化"
                )
            if project_workspace.sha256_file(context.annotation_path) != expected["annotationSha256"]:
                raise render_timing.RenderTimingError(
                    f"batch 发布前 {context.scene_id} current annotation 已变化"
                )
            if (
                not args.bare_tip
                and hand_png is not None
                and project_workspace.sha256_file(hand_png) != hand_sha256
            ):
                raise render_timing.RenderTimingError("batch 发布前 current hand 素材已变化")
            media = _publish_and_bind_scene(
                Path(worker_result["candidatePath"]),
                context.output_path,
                render_profile=project.render_profile,
                expected_frame_count=context.timing_scene["frameCount"],
                deep_receipt=worker_result["deepReceipt"],
            )
            manifest = render_timing.update_render_manifest(
                context,
                media=media,
                render_options=render_options,
            )
            identity = manifest["scenes"][context.scene_id]["renderIdentityHash"]
            try:
                expected["runDir"].rmdir()
            except OSError:
                pass
            results.append(
                {
                    "sceneId": context.scene_id,
                    "outputFile": context.output_path.relative_to(project.root).as_posix(),
                    "renderIdentityHash": identity,
                    "status": "published_current_technical",
                }
            )
        except Exception as exc:
            results.append(
                {
                    "sceneId": context.scene_id,
                    "outputFile": context.output_path.relative_to(project.root).as_posix(),
                    "status": "failed",
                    "stage": "coordinator_publish",
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                    "exitCode": _formal_error_exit_code(exc),
                }
            )
    success_count = sum(item["status"] == "published_current_technical" for item in results)
    failure_count = len(results) - success_count
    coordinator_publish_ms = _elapsed_ms(coordinator_publish_started_ns)
    summary = {
        "contractVersion": "whiteboard-scene-render-batch-v2",
        "status": "PASS" if failure_count == 0 else "FAIL",
        "partialSuccess": success_count > 0 and failure_count > 0,
        "projectId": project.project_id,
        "taskCount": len(contexts),
        "configured": configured,
        "effective": effective,
        "peak": peak,
        "configuredSceneRenderConcurrency": configured,
        "effectiveSceneRenderConcurrency": effective,
        "peakSceneRenderWorkers": peak,
        "sceneOrder": [context.scene_id for context in contexts],
        "results": results,
        "successCount": success_count,
        "failureCount": failure_count,
        "approvalWritten": False,
        "userConfirmationRequired": True,
    }
    summary.update(
        _runtime_metrics_summary(
            scene_metrics,
            batch_started_ns=batch_started_ns,
            context_ms=context_ms,
            prepare_ms=prepare_ms,
            candidate_execution_ms=candidate_execution_ms,
            coordinator_publish_ms=coordinator_publish_ms,
        )
    )
    return summary


def _formal_error_exit_code(exc: Exception) -> int:
    if isinstance(exc, annotation_review.AnnotationReviewApprovalRequired):
        return 5
    if isinstance(exc, (project_workspace.ProjectValidationError, project_workspace.WorkspaceError)):
        return 2
    if isinstance(exc, media_validation.MediaValidationError):
        return 4
    if isinstance(exc, render_timing.RenderTimingError):
        message = str(exc).casefold()
        stale_markers = (
            "stale",
            "approve-full",
            "approval identity",
            "identity 不匹配",
            "identity mismatch",
            "与 current timing plan 不一致",
            "与 current timing scene 不一致",
            "与 current narration.wav 不一致",
        )
        return 5 if any(marker in message for marker in stale_markers) else 2
    return 4


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.project or args.scene_id or args.all_scenes or args.scene_ids:
        try:
            if args.all_scenes or args.scene_ids:
                result = _run_formal_batch(args)
            else:
                final, identity = _run_formal(args)
        except (annotation_review.AnnotationReviewApprovalRequired,
                render_timing.RenderTimingError, project_workspace.ProjectValidationError,
                project_workspace.WorkspaceError,
                media_validation.MediaValidationError, RuntimeError, OSError) as exc:
            print(f"[err] {exc}")
            return _formal_error_exit_code(exc)
        if args.all_scenes or args.scene_ids:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            print(f"BATCH_STATUS={result['status']}")
            return 0 if result["status"] == "PASS" else 1
        print(f"RENDER_IDENTITY={identity}")
        print(f"OUTPUT={final}")
        return 0
    if not args.image or not args.annotation or not args.output:
        print("[err] standalone 渲染必须提供 image annotation output")
        return 2
    cfg = _build_cfg(args)

    print("=" * 56)
    print("SRT 白板动画整合渲染器 (mask 编排 + stream 画法)")
    print("=" * 56)

    image_bgr = sr._imread_any(args.image)
    if image_bgr is None:
        print(f"[err] 无法读取图片: {args.image}")
        return 1
    try:
        annotation = json.loads(Path(args.annotation).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[err] 无法读取标注: {e}")
        return 1
    if not annotation.get("elements"):
        print("[err] 标注中没有 elements")
        return 1

    total_ms = args.total_ms if args.total_ms is not None else annotation.get("sceneDurationMs")
    if not total_ms:
        last = max(e["reveal"]["startMs"] + e["reveal"]["durationMs"] for e in annotation["elements"])
        total_ms = last + 1000
    last = max(e["reveal"]["startMs"] + e["reveal"]["durationMs"] for e in annotation["elements"])
    if last > total_ms - 500:
        print(f"[err] 最后元素结束于 {last}ms，超过 total_ms - 500 ({total_ms - 500}ms)")
        return 2

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    hand_png = Path(args.hand) if args.hand else None
    renderer = RegionStreamRenderer(image_bgr, annotation, cfg, hand_png, args.bare_tip)
    print(f"  输入: {args.image}")
    print(f"  输出尺寸: {renderer.out_w}x{renderer.out_h}, 帧率: {cfg.fps}")
    print(f"  区域数: {len(annotation['elements'])}, 总时长: {total_ms}ms, "
          f"笔迹: {cfg.ink_path_mode}, 上色: {cfg.color_fill}")

    final = renderer.render_to(out_path, total_ms)

    size_mb = final.stat().st_size / (1024 * 1024)
    print(f"\n最终视频: {final}  ({size_mb:.2f} MB)")
    print("=" * 56)
    print(f"OUTPUT={final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
