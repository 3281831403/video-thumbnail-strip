"""缩略图条的行为契约测试（初始全部为红）。"""

import pytest

from thumbs.strip import (
    StripError,
    compose,
    layout_strip,
    pick_times,
    scale_thumbs,
    snap_to_keyframe,
    strip_batch,
    timecode_label,
    validate,
)


def test_even_sampling_keeps_last_thumbnail_on_final_frame():
    timeline = {"duration": 1000, "fps": 10, "keyframes": [0, 400, 900]}
    frame_ms = 1000.0 / timeline["fps"]

    times = pick_times(timeline, 3, "even")

    assert len(times) == 3
    assert times[0] == 0
    assert times[-1] < timeline["duration"]
    assert abs(times[-1] - (timeline["duration"] - frame_ms)) <= frame_ms


def test_keyframe_sampling_snaps_every_time_to_keyframe():
    assert snap_to_keyframe(1350, [0, 1000, 1333]) == 1333

    timeline = {"duration": 1000, "fps": 10, "keyframes": [0, 350, 700]}
    times = pick_times(timeline, 3, "keyframe")

    assert len(times) == 3
    assert times[0] == 0
    assert times[1] == 350
    assert times[2] == 700


def test_layout_last_row_does_not_overflow_columns():
    thumb_w, thumb_h, columns, gap = 120, 68, 2, 4
    positions = layout_strip(5, thumb_w, thumb_h, columns, gap)

    assert positions == [
        (0, 0),
        (124, 0),
        (0, 72),
        (124, 72),
        (0, 144),
    ]
    assert all(x <= (columns - 1) * (thumb_w + gap) for x, _ in positions)


def test_compose_clips_thumbnail_at_canvas_edge():
    canvas = [[0.0 for _ in range(4)] for _ in range(4)]
    thumb = {"w": 3, "h": 3, "data": [[1.0, 1.0, 1.0] for _ in range(3)]}

    out = compose(canvas, [thumb], [(2, 1)])

    assert len(out) == 4
    assert all(len(row) == 4 for row in out)
    assert out[1][2] == 1.0
    assert out[3][3] == 1.0
    assert out[3][0] == 0.0
    assert sum(cell == 1.0 for row in out for cell in row) == 6


def test_timecode_rounds_milliseconds_before_formatting():
    assert timecode_label(999.6, 30) == "00:00:01.000"
    assert timecode_label(3723004.0, 25) == "01:02:03.004"


def test_scale_keeps_aspect_ratio_when_boxes_differ():
    src = {
        "w": 2,
        "h": 8,
        "data": [[i / 10.0, i / 10.0] for i in range(8)],
    }

    out = scale_thumbs([src], 4, 4)[0]
    rows = [row[0] for row in out["data"]]

    assert (out["w"], out["h"]) == (4, 4)
    assert len(rows) == 4
    assert all(len(row) == 4 for row in out["data"])
    # 等比缩放系数为 2，居中裁切只覆盖源图中间几行，值应集中在
    # 0.2~0.5；非等比拉伸会把 0.0 和 0.7 也拉进输出。
    assert min(rows) >= 0.2
    assert max(rows) <= 0.5


def test_scale_center_crops_instead_of_top_left():
    src = {
        "w": 4,
        "h": 2,
        "data": [
            [0.1, 0.2, 0.3, 0.4],
            [0.1, 0.2, 0.3, 0.4],
        ],
    }

    out = scale_thumbs([src], 2, 2)[0]

    assert out["data"] == [[0.2, 0.3], [0.2, 0.3]]


def test_validate_reports_overlapping_thumbnails():
    strip = {
        "canvas": {"w": 100, "h": 100},
        "thumbs": [
            {"w": 60, "h": 60, "data": [[0.0] * 60 for _ in range(60)]},
            {"w": 60, "h": 60, "data": [[0.0] * 60 for _ in range(60)]},
        ],
        "layout": [(0, 0), (30, 30)],
        "times": [0, 1000],
        "labels": ["00:00:00.000", "00:00:01.000"],
        "fps": 25,
    }

    problems = validate(strip)

    assert any("重叠" in problem for problem in problems)


def test_batch_uses_fresh_canvas_for_each_video():
    config = {
        "count": 2,
        "mode": "even",
        "thumb_w": 8,
        "thumb_h": 8,
        "columns": 2,
        "gap": 0,
    }
    videos = [
        {"name": "a", "duration": 1000, "fps": 10, "keyframes": [0]},
        {"name": "b", "duration": 2000, "fps": 10, "keyframes": [0]},
    ]

    result = strip_batch(videos, config)
    strips = result["strips"]

    assert len(strips) == 2
    assert strips[0]["canvas"] is not strips[1]["canvas"]
    assert strips[0]["layout"] is not strips[1]["layout"]
    assert strips[0]["video"] == "a"
    assert strips[1]["video"] == "b"


def test_pick_times_rejects_zero_count():
    timeline = {"duration": 1000, "fps": 10, "keyframes": [0]}

    with pytest.raises(StripError):
        pick_times(timeline, 0, "even")


def test_pick_times_count_one_returns_midpoint():
    timeline = {"duration": 1000, "fps": 10, "keyframes": [0, 400, 900]}
    frame_ms = 1000.0 / timeline["fps"]

    times = pick_times(timeline, 1, "even")

    assert len(times) == 1
    assert abs(times[0] - timeline["duration"] / 2) <= frame_ms


def test_layout_with_zero_gap_is_compact():
    positions = layout_strip(3, 10, 8, 3, 0)

    assert positions == [(0, 0), (10, 0), (20, 0)]


def test_batch_with_empty_video_list_returns_empty():
    config = {
        "count": 3,
        "mode": "even",
        "thumb_w": 8,
        "thumb_h": 8,
        "columns": 2,
        "gap": 0,
    }

    assert strip_batch([], config) == {"strips": []}


def test_strip_batch_rejects_zero_count():
    config = {
        "count": 0,
        "mode": "even",
        "thumb_w": 8,
        "thumb_h": 8,
        "columns": 2,
        "gap": 0,
    }
    videos = [{"name": "a", "duration": 1000, "fps": 10, "keyframes": [0]}]

    with pytest.raises(StripError):
        strip_batch(videos, config)


def test_batch_preserves_video_order_on_success():
    config = {
        "count": 2,
        "mode": "even",
        "thumb_w": 8,
        "thumb_h": 8,
        "columns": 2,
        "gap": 0,
    }
    videos = [
        {"name": "a", "duration": 1000, "fps": 10, "keyframes": [0]},
        {"name": "b", "duration": 1500, "fps": 10, "keyframes": [0]},
        {"name": "c", "duration": 2000, "fps": 10, "keyframes": [0]},
    ]

    result = strip_batch(videos, config)

    assert [strip["video"] for strip in result["strips"]] == ["a", "b", "c"]
