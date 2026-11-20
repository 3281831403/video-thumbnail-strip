"""缩略图条生成器的契约实现。

时间单位统一为毫秒（int，距视频起点）；帧号从 0 开始，第 k 帧的起始
时间为 ``round(k * 1000 / fps)`` 毫秒。时长为 ``duration`` 毫秒、帧率
为 ``fps`` 时，可解码的最后一帧帧号为
``max(0, floor(duration * fps / 1000) - 1)``，任何取样结果都必须落在
``[0, duration)`` 内。除特别说明外，时间/帧号比较允许 1 帧容差。

时间轴 dict::

    {"duration": int, "fps": float, "keyframes": [int, ...]}

缩略图 dict::

    {"w": int, "h": int, "data": [[float, ...], ...]}

``data`` 为 ``h`` 行、每行 ``w`` 列的 0.0~1.0 灰度像素，行优先。

画布是 ``list[list[float]]``，同样行优先；布局项是 ``(x, y)``，表示
缩略图左上角坐标。所有公开函数只依赖标准库 ``math`` / ``bisect``。
"""

import bisect
import math

_MODES = ("even", "uniform", "keyframe")


class StripError(ValueError):
    """输入不满足缩略图条契约时抛出的异常。"""


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _is_number(value):
    """判断是否为可用的实数（排除 bool 与非有限浮点）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _round_half_up(value):
    """四舍五入（0.5 向上），返回 int。"""
    return int(math.floor(value + 0.5))


def _frame_time(frame, fps):
    """第 ``frame`` 帧的起始时间（毫秒）。"""
    return _round_half_up(frame * 1000.0 / fps)


def _frame_count(duration, fps):
    """可解码的帧数 ``n = max(1, floor(duration * fps / 1000))``。"""
    return max(1, int(math.floor(duration * fps / 1000.0)))


def _clamp_time(ms, duration):
    """把时间钳制到 ``[0, duration)``（duration 为 0 时只能取 0）。"""
    ms = int(ms)
    upper = int(math.floor(duration - 1)) if duration >= 1 else 0
    return max(0, min(ms, upper))


def _even_frames(frame_count, count):
    """在 ``0 .. frame_count-1`` 上等距取 ``count`` 个帧号（含首尾）。"""
    last = frame_count - 1
    if count == 1:
        return [frame_count // 2]
    frames = []
    denominator = 2 * (count - 1)
    for index in range(count):
        # 半进位：round(index * last / (count - 1))，避免浮点与截断误差。
        numerator = 2 * index * last + (count - 1)
        frame = numerator // denominator
        if frame > last:
            frame = last
        elif frame < 0:
            frame = 0
        frames.append(frame)
    return frames


def _uniform_frames(frame_count, count, duration, fps):
    """在 ``[0, duration - 1 帧]`` 上等距取帧号（含首尾）。"""
    frame_ms = 1000.0 / fps
    upper = duration - frame_ms
    if upper < 0:
        upper = 0.0
    if count == 1:
        points = [upper / 2.0]
    else:
        points = [index * upper / (count - 1) for index in range(count)]

    last = frame_count - 1
    frames = []
    for point in points:
        frame = _round_half_up(point * fps / 1000.0)
        frames.append(max(0, min(frame, last)))
    return frames


def _thumb_size(thumb, *, allow_empty=False):
    """校验并返回缩略图的 ``(w, h, data)``，非法时抛 StripError。"""
    if not isinstance(thumb, dict):
        raise StripError("缩略图必须是字典")
    try:
        width = thumb["w"]
        height = thumb["h"]
        data = thumb["data"]
    except KeyError as exc:
        raise StripError("缩略图缺少字段") from exc

    minimum = 0 if allow_empty else 1
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width < minimum
        or height < minimum
    ):
        raise StripError("缩略图尺寸非法")

    if not isinstance(data, (list, tuple)) or len(data) != height:
        raise StripError("缩略图 data 行数与 h 不符")
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) != width:
            raise StripError("缩略图 data 列数与 w 不符")
    return width, height, data


def _parse_timecode(text):
    """解析 ``HH:MM:SS.mmm``，非法时返回 None。"""
    if not isinstance(text, str):
        return None
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0], 10)
        minutes = int(parts[1], 10)
        seconds_text, dot, millis_text = parts[2].partition(".")
        seconds = int(seconds_text, 10)
        millis = int(millis_text, 10) if dot else 0
    except ValueError:
        return None
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    if dot and (not millis_text or len(millis_text) > 3):
        return None
    if dot:
        millis *= 10 ** (3 - len(millis_text))
    if not 0 <= millis < 1000:
        return None
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _timeline_parts(timeline):
    """从时间轴 dict 中取出并校验 ``(duration, fps, keyframes)``。"""
    if not isinstance(timeline, dict):
        raise StripError("时间轴必须是字典")
    try:
        duration = timeline["duration"]
        fps = timeline["fps"]
    except KeyError as exc:
        raise StripError("时间轴缺少 duration 或 fps") from exc

    if not _is_number(duration) or not _is_number(fps):
        raise StripError("duration/fps 必须是数字")
    if duration < 0:
        raise StripError("duration 不能为负")
    if fps <= 0:
        raise StripError("fps 必须为正")

    keyframes = timeline.get("keyframes", [])
    if keyframes is None:
        keyframes = []
    if not isinstance(keyframes, (list, tuple)):
        raise StripError("keyframes 必须是列表")
    return duration, fps, keyframes


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def pick_times(duration, count, mode):
    """按模式在时间轴上选取 ``count`` 个缩略图时间点（毫秒）。

    契约：
    - ``duration`` 参数是时间轴 dict
      ``{"duration": int ms, "fps": float, "keyframes": [int, ...]}``。
      记 d 为其 duration 字段（ms，>=0）、f 为 fps 字段（>0）。帧号从
      0 开始，共 ``n = max(1, floor(d * f / 1000))`` 帧，最后一帧帧号为
      ``n-1``，其时间为 ``round((n-1) * 1000 / f)``。所有返回值必须满足
      ``0 <= t < d``，首尾均不得越界。
    - ``mode="even"``：按帧号在 ``0 .. n-1`` 上等距取样并包含首尾帧；
      ``count=1`` 时只取中点帧 ``n // 2``。
    - ``mode="uniform"``：在时间区间 ``[0, d - 1 帧]`` 上等距取样
      （换算到帧号后取整），仍包含首尾；``count=1`` 时取区间中点。
    - ``mode="keyframe"``：先按 ``even`` 得到时间，再逐个吸附到
      时间轴 keyframes 中不超过该时间的最近关键帧；没有可用关键帧时
      取 0。
    - 时间比较允许 1 帧容差；不得使用会导致末帧提前 1 帧的整数除法。
    - ``count <= 0``、d < 0、f <= 0 或未知 mode 时抛
      :class:`StripError`。

    返回：``count`` 个 int 毫秒，按时间升序。
    """
    timeline_duration, fps, keyframes = _timeline_parts(duration)

    if isinstance(count, bool) or not isinstance(count, int):
        raise StripError("count 必须是正整数")
    if count <= 0:
        raise StripError("count 必须大于 0")
    if not isinstance(mode, str) or mode not in _MODES:
        raise StripError("未知的取样模式")

    frame_count = _frame_count(timeline_duration, fps)

    if mode == "uniform":
        frames = _uniform_frames(frame_count, count, timeline_duration, fps)
    else:
        frames = _even_frames(frame_count, count)

    times = []
    for frame in frames:
        times.append(_clamp_time(_frame_time(frame, fps), timeline_duration))

    if mode == "keyframe":
        times = [snap_to_keyframe(ms, keyframes) for ms in times]

    times.sort()
    return times


def snap_to_keyframe(ms, keyframes):
    """把时间 ``ms``（毫秒）吸附到不超过它的最近关键帧（毫秒）。

    ``keyframes`` 为升序的关键帧时间列表；返回 ``max(k <= ms)``，没有
    这样的 k 时返回 0。结果与 ``ms`` 的误差不超过 1 帧；``ms < 0`` 或
    关键帧为负时抛 :class:`StripError`。不修改入参。
    """
    if not _is_number(ms) or ms < 0:
        raise StripError("ms 必须为非负数")
    if not isinstance(keyframes, (list, tuple)):
        raise StripError("keyframes 必须是列表")

    previous = None
    for keyframe in keyframes:
        if not _is_number(keyframe):
            raise StripError("关键帧必须是数字")
        if keyframe < 0:
            raise StripError("关键帧不能为负")
        if previous is not None and keyframe < previous:
            raise StripError("关键帧必须升序")
        previous = keyframe

    index = bisect.bisect_right(keyframes, ms) - 1
    if index < 0:
        return 0
    return int(keyframes[index])


def layout_strip(count, thumb_w, thumb_h, columns, gap):
    """生成 ``count`` 个缩略图的左上角坐标 ``(x, y)``，行优先。

    - 缩略图尺寸固定为 ``thumb_w x thumb_h``（像素）。
    - 每行最多 ``columns`` 个；第 i 个：row = i // columns，
      col = i % columns，x = col * (thumb_w + gap)，
      y = row * (thumb_h + gap)。
    - 最后一行不足 ``columns`` 个时，其列号仍必须小于 ``columns``，
      不得溢出到画布外；相邻缩略图间水平/垂直间距均为 ``gap``，矩形
      互不重叠。
    - ``count=0`` 返回 ``[]``；``thumb_w/thumb_h <= 0``、``columns <= 0``
      或 ``gap < 0`` 抛 :class:`StripError`。
    """
    for value, name in (
        (thumb_w, "thumb_w"),
        (thumb_h, "thumb_h"),
        (columns, "columns"),
        (gap, "gap"),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise StripError(f"{name} 必须是整数")
    if thumb_w <= 0 or thumb_h <= 0:
        raise StripError("缩略图尺寸必须为正")
    if columns <= 0:
        raise StripError("columns 必须为正")
    if gap < 0:
        raise StripError("gap 不能为负")

    if count <= 0:
        return []

    stride_x = thumb_w + gap
    stride_y = thumb_h + gap
    positions = []
    for index in range(count):
        row, column = divmod(index, columns)
        positions.append((column * stride_x, row * stride_y))
    return positions


def compose(canvas, thumbs, layout):
    """按 ``layout`` 把 ``thumbs`` 合入 ``canvas``，返回新画布。

    - ``canvas`` 为 h 行 w 列的 float 矩阵；``thumbs[i]`` 为
      ``{"w", "h", "data"}``；``layout[i]`` 为 ``(x, y)`` 左上角。
    - 逐像素复制 ``data[ty][tx]`` 到 ``canvas[y+ty][x+tx]``。
    - 超出画布边界的部分必须裁剪（不得抛 IndexError），左侧/上方越界
      同理裁掉；画布尺寸与入参保持一致，入参不被修改。
    - ``len(thumbs) != len(layout)``、画布非矩形或缩略图 data 尺寸与
      w/h 不符时抛 :class:`StripError`。
    """
    if not isinstance(canvas, (list, tuple)):
        raise StripError("canvas 必须是矩阵")
    height = len(canvas)
    width = len(canvas[0]) if height else 0
    for row in canvas:
        if not isinstance(row, (list, tuple)) or len(row) != width:
            raise StripError("canvas 必须是非空矩形矩阵")

    if not isinstance(thumbs, (list, tuple)) or not isinstance(layout, (list, tuple)):
        raise StripError("thumbs/layout 必须是列表")
    if len(thumbs) != len(layout):
        raise StripError("thumbs 与 layout 数量不符")

    normalized_thumbs = []
    for thumb in thumbs:
        normalized_thumbs.append(_thumb_size(thumb, allow_empty=True))

    positions = []
    for position in layout:
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            raise StripError("布局项必须是 (x, y)")
        x, y = position
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            raise StripError("布局坐标必须是整数")
        positions.append((x, y))

    out = [list(row) for row in canvas]

    for (thumb_w, thumb_h, data), (x, y) in zip(normalized_thumbs, positions):
        for ty in range(thumb_h):
            canvas_y = y + ty
            if canvas_y < 0 or canvas_y >= height:
                continue
            source_row = data[ty]
            destination_row = out[canvas_y]
            for tx in range(thumb_w):
                canvas_x = x + tx
                if canvas_x < 0 or canvas_x >= width:
                    continue
                destination_row[canvas_x] = source_row[tx]

    return out


def timecode_label(ms, fps):
    """把毫秒时间格式化为 ``HH:MM:SS.mmm``（小时可超过 23）。

    - ``ms`` 可以为 float；先四舍五入到最近整毫秒（0.5 向上），再向
      秒/分/时进位，毫秒固定三位。例：999.6 -> ``"00:00:01.000"``。
    - ``fps > 0`` 用于校验时间轴帧率；本函数不改变输入时间。时间比较
      仍遵循 1 帧容差。
    - ``ms < 0`` 或 ``fps <= 0`` 抛 :class:`StripError`。
    """
    if not _is_number(ms) or ms < 0:
        raise StripError("ms 必须为非负数")
    if not _is_number(fps) or fps <= 0:
        raise StripError("fps 必须为正")

    total_ms = _round_half_up(ms)
    millis = total_ms % 1000
    total_seconds = total_ms // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def scale_thumbs(thumbs, thumb_w, thumb_h):
    """把每个缩略图等比缩放后居中裁切为 ``thumb_w x thumb_h``。

    - 输入缩略图 ``{"w", "h", "data"}``；输出同结构，w/h 必须等于目标。
    - 覆盖式缩放：``s = max(thumb_w / w, thumb_h / h)``，缩放后尺寸
      至少覆盖目标框；禁止非等比拉伸。
    - 以缩放图中心为基准裁切，偏移为
      ``((sw - thumb_w) // 2, (sh - thumb_h) // 2)``，不得固定取左上角。
    - 使用最近邻取样（直接复制源像素值）；空列表返回 ``[]``；尺寸或
      data 非法时抛 :class:`StripError`。缩放/裁切的位置误差容限为
      1 像素。
    """
    for value, name in ((thumb_w, "thumb_w"), (thumb_h, "thumb_h")):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise StripError(f"{name} 必须为正整数")

    if not isinstance(thumbs, (list, tuple)):
        raise StripError("thumbs 必须是列表")
    if not thumbs:
        return []

    scaled = []
    for thumb in thumbs:
        source_w, source_h, data = _thumb_size(thumb)

        scale = max(thumb_w / float(source_w), thumb_h / float(source_h))
        # 至少覆盖目标框（ceil 防止浮点误差把尺寸缩小 1 像素）。
        scaled_w = max(thumb_w, int(math.ceil(source_w * scale - 1e-9)))
        scaled_h = max(thumb_h, int(math.ceil(source_h * scale - 1e-9)))

        offset_x = (scaled_w - thumb_w) // 2
        offset_y = (scaled_h - thumb_h) // 2

        rows = []
        for out_y in range(thumb_h):
            source_y = int(math.floor((out_y + offset_y) / scale))
            source_y = max(0, min(source_y, source_h - 1))
            source_row = data[source_y]
            out_row = []
            for out_x in range(thumb_w):
                source_x = int(math.floor((out_x + offset_x) / scale))
                source_x = max(0, min(source_x, source_w - 1))
                out_row.append(source_row[source_x])
            rows.append(out_row)
        scaled.append({"w": thumb_w, "h": thumb_h, "data": rows})

    return scaled


def validate(strip):
    """检查一条缩略图条，返回问题描述列表；合法时返回 ``[]``。

    ``strip`` 字段::

        {"canvas": {"w": int, "h": int},
         "thumbs": [thumbnail, ...],
         "layout": [(x, y), ...],
         "times": [int, ...],
         "labels": [str, ...],
         "fps": float}

    检查项（问题字符串分别以 ``数量不符`` / ``越界`` / ``重叠`` /
    ``标签错位`` 开头）：
    - thumbs/layout/times/labels 数量必须一致；
    - 每个缩略图矩形必须完全位于画布内（含边界），坐标非负；
    - 任意两个矩形不得有面积大于 0 的交叠；仅边/角相接不算重叠；
    - labels[i] 必须等于 ``timecode_label(times[i], fps)``（允许 1 帧
      时间容差）。
    返回全部发现的问题，不抛异常（字段缺失按数量不符处理）。
    """
    problems = []
    if not isinstance(strip, dict):
        return ["数量不符: strip 必须是字典"]

    required = ("canvas", "thumbs", "layout", "times", "labels", "fps")
    missing = [name for name in required if name not in strip]
    if missing:
        problems.append("数量不符: 缺少字段 " + ", ".join(missing))

    thumbs = strip.get("thumbs", [])
    layout = strip.get("layout", [])
    times = strip.get("times", [])
    labels = strip.get("labels", [])

    sequence_ok = all(isinstance(value, (list, tuple)) for value in (thumbs, layout, times, labels))
    if not sequence_ok:
        problems.append("数量不符: thumbs/layout/times/labels 必须是列表")
        return problems

    if not (len(thumbs) == len(layout) == len(times) == len(labels)):
        problems.append(
            "数量不符: thumbs={}, layout={}, times={}, labels={}".format(
                len(thumbs), len(layout), len(times), len(labels)
            )
        )

    # 画布尺寸：支持 {"w", "h"} 与像素矩阵两种形式。
    canvas = strip.get("canvas")
    canvas_w = canvas_h = None
    if isinstance(canvas, dict):
        width = canvas.get("w")
        height = canvas.get("h")
        if isinstance(width, int) and isinstance(height, int):
            canvas_w, canvas_h = width, height
        else:
            problems.append("数量不符: canvas 缺少有效尺寸")
    elif isinstance(canvas, (list, tuple)):
        canvas_h = len(canvas)
        if canvas_h:
            first = canvas[0]
            canvas_w = len(first) if isinstance(first, (list, tuple)) else 0
            if not isinstance(first, (list, tuple)) or any(
                not isinstance(row, (list, tuple)) or len(row) != canvas_w for row in canvas
            ):
                problems.append("数量不符: canvas 不是矩形矩阵")
                canvas_w = canvas_h = None
        else:
            canvas_w = 0
    elif "canvas" not in strip:
        pass
    else:
        problems.append("数量不符: canvas 格式非法")

    rects = []
    for index in range(min(len(thumbs), len(layout))):
        thumb = thumbs[index]
        position = layout[index]
        try:
            thumb_w, thumb_h, _ = _thumb_size(thumb, allow_empty=True)
        except StripError:
            problems.append(f"数量不符: 缩略图 {index} 尺寸或 data 非法")
            continue

        if not isinstance(position, (list, tuple)) or len(position) != 2:
            problems.append(f"数量不符: 布局项 {index} 非法")
            continue
        x, y = position
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            problems.append(f"数量不符: 布局项 {index} 坐标非法")
            continue

        if canvas_w is not None and canvas_h is not None:
            if x < 0 or y < 0 or x + thumb_w > canvas_w or y + thumb_h > canvas_h:
                problems.append(
                    f"越界: 缩略图 {index} 位于 ({x}, {y})，尺寸 {thumb_w}x{thumb_h}"
                    f" 超出画布 {canvas_w}x{canvas_h}"
                )
        rects.append((index, x, y, thumb_w, thumb_h))

    for first_index in range(len(rects)):
        index_a, x_a, y_a, w_a, h_a = rects[first_index]
        for second_index in range(first_index + 1, len(rects)):
            index_b, x_b, y_b, w_b, h_b = rects[second_index]
            overlap_x = min(x_a + w_a, x_b + w_b) - max(x_a, x_b)
            overlap_y = min(y_a + h_a, y_b + h_b) - max(y_a, y_b)
            if overlap_x > 0 and overlap_y > 0:
                problems.append(f"重叠: 缩略图 {index_a} 与 {index_b} 交叠")

    fps = strip.get("fps")
    if _is_number(fps) and fps > 0:
        frame_ms = 1000.0 / fps
        for index in range(min(len(labels), len(times))):
            label = labels[index]
            time = times[index]
            parsed = _parse_timecode(label)
            if parsed is None:
                problems.append(f"标签错位: 第 {index} 个标签无法解析")
                continue
            if not _is_number(time):
                problems.append(f"标签错位: 第 {index} 个时间非法")
                continue
            if abs(parsed - time) > frame_ms + 1e-9:
                expected = timecode_label(time, fps)
                problems.append(
                    f"标签错位: 第 {index} 个标签为 {label!r}，应为 {expected!r}"
                )
    elif _is_number(fps):
        problems.append("标签错位: fps 必须为正")

    return problems


def strip_batch(videos, config):
    """批量生成缩略图条；任一时间轴非法则整体失败、不返回部分结果。

    - ``videos``：时间轴 dict 列表，可带 ``name``（缺省用下标字符串）；
      空列表返回 ``{"strips": []}``。
    - ``config``：``{"count", "mode", "thumb_w", "thumb_h", "columns",
      "gap"}``。
    - 每个视频独立创建画布和布局，禁止复用/叠加其他视频的画布。
    - 返回 ``{"strips": [strip, ...]}``，顺序与输入 videos 完全一致；
      每个 strip 含 video、canvas、thumbs、layout、times、labels、fps。
    - 任一视频/参数不合法（含 count=0）抛 :class:`StripError`；原子性
      要求：失败时不产生、不泄漏任何已完成的部分结果。
    - 时间/帧号/缩略图尺寸/1 帧容差语义同前。
    """
    if not isinstance(videos, (list, tuple)):
        raise StripError("videos 必须是列表")
    if not isinstance(config, dict):
        raise StripError("config 必须是字典")

    try:
        count = config["count"]
        mode = config["mode"]
        thumb_w = config["thumb_w"]
        thumb_h = config["thumb_h"]
        columns = config["columns"]
        gap = config["gap"]
    except KeyError as exc:
        raise StripError("config 缺少字段") from exc

    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise StripError("count 必须为正整数")
    if not isinstance(mode, str) or mode not in _MODES:
        raise StripError("未知的取样模式")
    for value, name in (
        (thumb_w, "thumb_w"),
        (thumb_h, "thumb_h"),
        (columns, "columns"),
        (gap, "gap"),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise StripError(f"{name} 必须是整数")
    if thumb_w <= 0 or thumb_h <= 0:
        raise StripError("缩略图尺寸必须为正")
    if columns <= 0:
        raise StripError("columns 必须为正")
    if gap < 0:
        raise StripError("gap 不能为负")

    if not videos:
        return {"strips": []}

    rows = (count + columns - 1) // columns
    canvas_w = columns * thumb_w + (columns - 1) * gap
    canvas_h = rows * thumb_h + (rows - 1) * gap

    strips = []
    for index, video in enumerate(videos):
        if not isinstance(video, dict):
            raise StripError("视频时间轴必须是字典")
        try:
            fps = video["fps"]
        except KeyError as exc:
            raise StripError("视频时间轴缺少 fps") from exc
        if not _is_number(fps) or fps <= 0:
            raise StripError("fps 必须为正")

        times = pick_times(video, count, mode)
        labels = [timecode_label(time, fps) for time in times]

        # 每个视频都拥有独立、全新的画布/缩略图/布局，互不叠加。
        positions = layout_strip(count, thumb_w, thumb_h, columns, gap)
        thumbs = [
            {
                "w": thumb_w,
                "h": thumb_h,
                "data": [[0.0] * thumb_w for _ in range(thumb_h)],
            }
            for _ in range(count)
        ]
        canvas = [[0.0] * canvas_w for _ in range(canvas_h)]
        canvas = compose(canvas, thumbs, positions)

        strips.append(
            {
                "video": video["name"] if "name" in video else str(index),
                "canvas": canvas,
                "thumbs": thumbs,
                "layout": positions,
                "times": times,
                "labels": labels,
                "fps": fps,
            }
        )

    return {"strips": strips}
