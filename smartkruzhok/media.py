import json
import math
import os
import re
import subprocess
import sys
import wave
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import effects as visual_effects

SIZE = 480
FPS = 24
DETECT_FPS = 6
MAX_SECONDS = 180
MAX_BYTES = 20_000_000
PART_FRAMES = 60 * FPS
CIRCLE = np.fromfunction(
    lambda y, x: (x - (SIZE - 1) / 2) ** 2 + (y - (SIZE - 1) / 2) ** 2 < (SIZE / 2) ** 2,
    (SIZE, SIZE),
)
cv2.setNumThreads(2)


class MediaError(Exception):
    pass


@dataclass
class Video:
    width: int
    height: int
    frames: int
    seconds: float
    audio: bool

    def parts(self, series, fragment=None, pauses=()):
        if fragment:
            return [tuple(fragment)]
        end = self.frames if series else min(self.frames, PART_FRAMES)
        parts = []
        start = 0
        while start < end:
            limit = min(start + PART_FRAMES, end)
            if limit < end:
                limit = max((pause for pause in pauses if limit - 8 * FPS <= pause <= limit), default=limit)
            parts.append((start, limit))
            start = limit
        return parts


def speech_pauses(path):
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", str(path),
        "-map", "0:a:0", "-af", "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-",
    ], capture_output=True, text=True, check=True, timeout=60)
    pauses = []
    start = None
    for event, value in re.findall(r"silence_(start|end): (-?[\d.]+)", result.stderr):
        if event == "start":
            start = float(value)
        elif start is not None:
            end = float(value)
            # Either edge can be too far from the time limit when silence is long.
            midpoint = (start + end) / 2
            pauses.extend(round(time * FPS) for time in (start + 0.15, midpoint, end - 0.15))
            start = None
    return sorted(set(pauses))


def probe(path):
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist",
            "mov,matroska,webm", "-show_streams", "-show_format", "-of", "json", str(path),
        ], capture_output=True, check=True, timeout=20)
        info = json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError) as error:
        raise MediaError("Не удалось прочитать видео. Попробуй отправить другой файл.") from error
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None or not ({"mov", "webm"} & set(info["format"]["format_name"].split(","))):
        raise MediaError("Отправь видео в формате MP4, MOV или WebM.")
    try:
        seconds = float(video.get("duration") or info["format"]["duration"])
        width, height = video["width"], video["height"]
        aspect = video.get("sample_aspect_ratio", "1:1").split(":")
        if len(aspect) == 2 and all(int(value) > 0 for value in aspect):
            width *= int(aspect[0]) / int(aspect[1])
        angle = next((int(s["rotation"]) for s in video.get("side_data_list", []) if "rotation" in s), 0)
    except (KeyError, ValueError, TypeError, OverflowError) as error:
        raise MediaError("Не удалось определить длительность или размер видео.") from error
    if not math.isfinite(seconds) or seconds <= 0 or min(width, height) <= 0:
        raise MediaError("Не удалось определить длительность или размер видео.")
    if seconds > MAX_SECONDS:
        raise MediaError("Видео длиннее 3 минут. Сократи его и отправь снова.")
    if angle % 180 == 90:
        width, height = height, width
    scale = min(1, 640 / max(width, height))
    return Video(max(2, round(width * scale)), max(2, round(height * scale)),
                 max(1, math.ceil(seconds * FPS - 1e-7)), seconds,
                 any(s.get("codec_type") == "audio" for s in streams))


@contextmanager
def video_frames(path, width, height, fps, first, last):
    count = last - first
    command = [
        "ffmpeg", "-v", "error", "-xerror", "-nostdin", "-threads", "2",
        "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:V:0", "-an", "-filter_threads", "2",
        "-vf", f"fps={fps}:start_time=0,tpad=stop_mode=clone:stop_duration=1,"
        f"trim=start_frame={first}:end_frame={last},setpts=PTS-STARTPTS,scale={width}:{height}",
        "-frames:v", str(count), "-c:v", "rawvideo", "-threads", "1", "-pix_fmt", "bgr24",
        "-f", "rawvideo", "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = width * height * 3

    def read_frames():
        for _ in range(count):
            data = process.stdout.read(size)
            if len(data) != size:
                raise MediaError("Не удалось декодировать видео полностью. Проверь исходный файл.")
            yield np.frombuffer(data, np.uint8).reshape(height, width, 3)
        if process.wait(timeout=15):
            raise MediaError("Не удалось декодировать видео. Проверь исходный файл.")

    try:
        yield read_frames()
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        process.wait()


@lru_cache(maxsize=4)
def detector(width, height):
    model = os.getenv("FACE_MODEL")
    if model:
        return cv2.FaceDetectorYN.create(model, "", (width, height), 0.72, 0.3, 50)
    return cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


def find_faces(image):
    height, width = image.shape[:2]
    if min(width, height) < 24:
        return []
    model = detector(width, height)
    if isinstance(model, cv2.FaceDetectorYN):
        _, detections = model.detect(image)
        return [] if detections is None else [tuple(map(float, row[:4])) for row in detections]
    gray = cv2.equalizeHist(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    return [tuple(map(float, box)) for box in model.detectMultiScale(gray, 1.12, 5, minSize=(24, 24))]


def face_center(box):
    x, y, width, height = box
    return x + width / 2, y + height / 2


def match_score(box, previous):
    distance = math.dist(face_center(box), face_center(previous))
    size_change = abs(math.log((box[2] * box[3]) / (previous[2] * previous[3])))
    return distance / max(20, previous[2], previous[3]) + 0.35 * size_change


def select_track(samples):
    track = []
    previous = None
    missing = 0
    for index, faces in enumerate(samples):
        match = None
        if previous is not None:
            if faces:
                candidate = min(faces, key=lambda box: match_score(box, previous))
                if match_score(candidate, previous) < 0.85:
                    match = candidate
            missing = missing + 1 if match is None else 0
            if missing <= 3 and match is None:
                track.append(previous)
                continue
            if match is None:
                previous = None
        if match is None and faces:
            following = samples[index + 1:index + 4]

            def prominence(box):
                sightings = sum(any(match_score(other, box) < 0.85 for other in group)
                                for group in following)
                return (box[2] * box[3]) ** 0.6 * (1 + sightings) if sightings else 0

            candidate = max(faces, key=prominence)
            if prominence(candidate):
                match = candidate
        track.append(match)
        previous = match
        if match is not None:
            missing = 0
    return track


def face_samples(path, video, cache):
    if cache.exists():
        return json.loads(cache.read_text())
    samples = []
    with video_frames(path, video.width, video.height, DETECT_FPS, 0,
                      math.ceil(video.frames / FPS * DETECT_FPS)) as frames:
        for frame in frames:
            samples.append(sorted(find_faces(frame), key=lambda box: (box[0], box[1]))[:8])
    save_json(cache, samples)
    return samples


def follow_face(samples, index, seed):
    def follow(groups):
        previous = seed
        missing = 0
        for faces in groups:
            candidate = min(faces, key=lambda box: match_score(box, previous), default=None)
            if candidate is not None and match_score(candidate, previous) < 0.85:
                missing = 0
                previous = candidate
            else:
                missing += 1
                if missing > 3:
                    # A new detection after a long gap may be a different person.
                    break
            yield previous

    before = list(follow(reversed(samples[:index])))
    after = list(follow(samples[index + 1:]))
    return ([None] * (index - len(before)) + before[::-1] + [seed] + after
            + [None] * (len(samples) - index - 1 - len(after)))


def analyze(path, video, cache, *, selected=None):
    if cache.exists():
        return json.loads(cache.read_text())
    samples = face_samples(path, video, cache.with_name("faces.json"))
    if selected is None:
        track = select_track(samples)
    else:
        index, option = selected
        track = follow_face(samples, index, samples[index][option])
    center = np.array([video.width / 2, video.height / 2])
    targets = np.array([face_center(box) if box else center for box in track])
    smooth = targets.copy()
    for index in range(1, len(smooth)):
        smooth[index] = 0.72 * smooth[index - 1] + 0.28 * targets[index]
    for index in range(len(smooth) - 2, -1, -1):
        smooth[index] = 0.65 * smooth[index + 1] + 0.35 * smooth[index]
    trajectory = [[point.tolist(), box] for point, box in zip(smooth, track)]
    save_json(cache, trajectory)
    return trajectory


def save_json(cache, value):
    pending = cache.with_suffix(".tmp")
    pending.write_text(json.dumps(value))
    pending.replace(cache)


def face_photo(path, video, sample, faces, output):
    with video_frames(path, video.width, video.height, DETECT_FPS, sample, sample + 1) as frames:
        frame = next(frames).copy()
    scale = max(1.0, 480 / max(video.width, video.height))
    if scale > 1:
        frame = cv2.resize(frame, (round(video.width * scale), round(video.height * scale)))
    height, width = frame.shape[:2]
    for index, box in enumerate(faces, 1):
        x, y, w, h = (round(value * scale) for value in box)
        cv2.rectangle(frame, (x, y), (x + w, y + h), (55, 224, 255), 2)
        label_x = min(max(0, x), max(0, width - 25))
        label_y = min(max(22, y), height - 2)
        cv2.rectangle(frame, (label_x, label_y - 22), (label_x + 25, label_y), (55, 224, 255), -1)
        cv2.putText(frame, str(index), (label_x + 5, label_y - 5), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (25, 25, 25), 2, cv2.LINE_AA)
    if not cv2.imwrite(str(output), frame):
        raise MediaError("Не удалось сохранить кадр с лицами.")


def sample_track(trajectory, frame_number):
    if not trajectory:
        return None, None
    sample = frame_number * DETECT_FPS / FPS
    low = min(int(sample), len(trajectory) - 1)
    high = min(low + 1, len(trajectory) - 1)
    center_a, box_a = trajectory[low]
    center_b, box_b = trajectory[high]
    fraction = sample - int(sample)
    center = np.array(center_a) * (1 - fraction) + np.array(center_b) * fraction
    if box_a and box_b and match_score(box_b, box_a) < 0.85:
        box = np.array(box_a) * (1 - fraction) + np.array(box_b) * fraction
    else:
        box = box_a if fraction < 0.5 else box_b
    return center, box


def square_crop(frame, center, side):
    height, width = frame.shape[:2]
    side = min(max(2, round(side)), width, height)
    x = int(np.clip(round(center[0] - side / 2), 0, width - side))
    y = int(np.clip(round(center[1] - side / 2), 0, height - side))
    return cv2.resize(frame[y:y + side, x:x + side], (SIZE, SIZE)), x, y, SIZE / side


def box_in_frame(box, x, y, scale):
    if box is None:
        return None
    return (box[0] * scale + x, box[1] * scale + y, box[2] * scale, box[3] * scale)


def active_effects(settings):
    if "effects" in settings:
        return settings["effects"]
    legacy = settings.get("effect")
    return [legacy] if legacy in ("retro", "zoom") else []


def compose_frame(frame, mode, effects, frame_number, trajectory, zoom_start=0):
    height, width = frame.shape[:2]
    center = (width / 2, height / 2)
    tracked, box = sample_track(trajectory, frame_number)
    if mode == "fit":
        canvas, _, _, _ = square_crop(frame, center, min(width, height))
        canvas = cv2.GaussianBlur(canvas, (0, 0), 17)
        scale = (SIZE - 16) / math.hypot(width, height)
        w, h = max(1, int(width * scale)), max(1, int(height * scale))
        x, y = (SIZE - w) // 2, (SIZE - h) // 2
        canvas[y:y + h, x:x + w] = cv2.resize(frame, (w, h))
        face = box_in_frame(box, x, y, scale)
    else:
        focus = tracked if mode == "smart" and tracked is not None else center
        if mode == "smart" and box is not None:
            face_x, face_y = face_center(box)
            tolerance = max(0, min(width, height) * 0.30 - math.hypot(box[2], box[3]) / 2)
            focus = (float(np.clip(focus[0], face_x - tolerance, face_x + tolerance)),
                     float(np.clip(focus[1], face_y - tolerance, face_y + tolerance)))
        canvas, x, y, scale = square_crop(frame, focus, min(width, height))
        face = box_in_frame(box, -x * scale, -y * scale, scale)
    elapsed = frame_number - zoom_start
    if "zoom" in effects and 0 <= elapsed < 2 * FPS:
        phase = math.sin(math.pi * elapsed / (2 * FPS)) ** 2
        focus = face_center(face) if face is not None else (SIZE / 2, SIZE / 2)
        side = SIZE / (1 + 0.32 * phase)
        zoomed, x, y, scale = square_crop(canvas, focus, side)
        zoom_face = box_in_frame(face, -x * scale, -y * scale, scale)
        if zoom_face is None or inside_circle(zoom_face):
            canvas, face = zoomed, zoom_face
    if "retro" in effects:
        blue, green, red = cv2.split(canvas)
        blue = cv2.addWeighted(blue, 0.82, np.roll(blue, 2, axis=1), 0.18, 8)
        red = cv2.addWeighted(red, 0.72, np.roll(red, -3, axis=1), 0.28, 10)
        canvas = cv2.merge((blue, green, red))
        canvas[(frame_number // 2) % 4::4] //= 2
    return canvas, face


def inside_circle(box, margin=10):
    x, y, width, height = box
    return all(math.hypot(cx - SIZE / 2, cy - SIZE / 2) <= SIZE / 2 - margin
               for cx in (x, x + width) for cy in (y, y + height))


@lru_cache(maxsize=4)
def font(size):
    paths = [
        os.getenv("FONT_PATH"), "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for path in paths:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise MediaError("На сервере не найден шрифт для надписей.")


def wrap(text, size):
    face = font(size)
    lines = []
    line = ""
    for word in text.split():
        if face.getlength(word) > 290:
            return []
        candidate = f"{line} {word}".strip()
        if face.getlength(candidate) <= 290:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines if len(lines) <= 2 else []


def overlaps(a, b):
    return (a[0] < b[0] + b[2] and a[0] + a[2] > b[0]
            and a[1] < b[1] + b[3] and a[1] + a[3] > b[1])


@lru_cache(maxsize=96)
def text_overlay(text, y, size, highlighted=-1, *, compact=False):
    lines = wrap(text, size)
    if not lines and compact:
        size = 23
        lines = wrap(text, size)
    if not lines:
        return None
    face = font(size)
    step = size + 6
    width = math.ceil(max(face.getlength(line) for line in lines)) + 20
    height = len(lines) * step + 10
    x, top = (SIZE - width) // 2, y - height // 2
    layer = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=11, fill=(12, 12, 16, 215))
    word_index = 0
    for index, line in enumerate(lines):
        left = (width - face.getlength(line)) / 2
        baseline = 5 + index * step
        if highlighted < 0:
            draw.text((left, baseline), line, font=face, fill="white", anchor="lt")
            continue
        prefix = ""
        for word in line.split():
            color = "#ffdb58" if word_index == highlighted else "white"
            draw.text((left + face.getlength(prefix), baseline), word, font=face, fill=color, anchor="lt")
            prefix += word + " "
            word_index += 1
    rgba = np.asarray(layer)
    return (x, top, width, height), rgba[:, :, :3][:, :, ::-1], rgba[:, :, 3:4].astype(np.float32) / 255


def decorate(canvas, face, title, position, caption, highlighted):
    title_y = 105 if position == "top" else 375
    title_layer = text_overlay(title, title_y, 29) if title else None
    layers = [title_layer] if title_layer else []
    if caption:
        positions = (375, 105) if not title_layer or position == "top" else (105, 375)
        caption_layer = None
        for y in positions:
            candidate = text_overlay(caption, y, 29, highlighted, compact=True)
            if candidate is None:
                break
            if title_layer and overlaps(candidate[0], title_layer[0]):
                continue
            caption_layer = candidate
            if face is None or not overlaps(candidate[0], face):
                break
        if caption_layer is not None:
            layers.append(caption_layer)
    if not layers:
        return canvas
    canvas = canvas.copy()
    for (x, y, width, height), pixels, alpha in layers:
        region = canvas[y:y + height, x:x + width]
        region[:] = region * (1 - alpha) + pixels * alpha
    return canvas


def transcribe(path, seconds, audio_path, *, start=0):
    subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file,pipe",
        "-ss", str(start), "-i", str(path), "-t", str(seconds - start), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
        "-af", "aresample=async=1:first_pts=0", str(audio_path),
    ], check=True, stderr=subprocess.DEVNULL, timeout=60)
    with wave.open(str(audio_path)) as audio:
        if not audio.getnframes():
            return []
    # Separate OpenCV's bundled FFmpeg from the copy loaded by Whisper's PyAV.
    speech = subprocess.run(
        [sys.executable, "-m", "smartkruzhok.speech", str(audio_path)],
        capture_output=True, check=True, timeout=300,
    )
    lines = []
    for segment in json.loads(speech.stdout):
        words = []
        for word_start, word_end, value in segment["words"]:
            word_start += start
            word_end += start
            value = value.strip()
            if not value or not wrap(value, 23):
                continue
            candidate = " ".join([item[2] for item in words] + [value])
            if words and (not wrap(candidate, 29) or len(words) == 6 or word_start - words[-1][1] > 0.55):
                add_line(lines, words, seconds)
                words = []
            words.append([word_start, word_end, value])
        if words:
            add_line(lines, words, seconds)
        elif not segment["words"] and wrap(segment["text"].strip(), 23):
            if segment["start"] + start < min(seconds, segment["end"] + start):
                lines.append({"start": segment["start"] + start, "end": min(seconds, segment["end"] + start),
                              "text": segment["text"].strip(), "words": []})
    return lines


def add_line(lines, words, seconds):
    start = words[0][0]
    end = min(seconds, words[-1][1])
    if end > start:
        lines.append({"start": start, "end": end, "text": " ".join(word[2] for word in words), "words": words})


def caption_at(lines, starts, mode, seconds):
    if mode not in ("plain", "words"):
        return "", -1
    index = bisect_right(starts, seconds) - 1
    if index < 0 or seconds >= lines[index]["end"]:
        return "", -1
    line = lines[index]
    active = -1
    if mode == "words":
        active = next((i for i, (start, end, _) in enumerate(line["words"])
                       if start <= seconds < end), -1)
    return line["text"], active


def frame_for(frame, number, settings, trajectory, lines, starts, mask):
    effects = active_effects(settings)
    start = settings.get("zoom_start", 0)
    elapsed = (number - start) / FPS
    canvas, face = compose_frame(frame, settings["mode"], effects, number, trajectory, start)
    if "film" in effects:
        canvas = visual_effects.film_frame(canvas, number)
    if "monochrome" in settings and elapsed >= settings["monochrome"]:
        canvas = cv2.cvtColor(cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    if border := settings.get("border"):
        visual_effects.add_border(canvas, border)
    visual_effects.add_stickers(canvas, settings.get("stickers", {}), elapsed)
    caption, highlighted = caption_at(lines, starts, settings.get("subtitles"), number / FPS)
    canvas = decorate(canvas, face, settings.get("title", ""), settings.get("position"), caption, highlighted)
    if mask:
        canvas[~CIRCLE] = (26, 25, 24)
    return canvas


def photo(path, video, number, settings, trajectory, output):
    with video_frames(path, video.width, video.height, FPS, number, number + 1) as frames:
        for frame in frames:
            canvas = frame_for(frame, number, settings, trajectory, [], [], True)
    if not cv2.imwrite(str(output), canvas):
        raise MediaError("Не удалось сохранить предпросмотр.")


def render(path, video, first, last, settings, trajectory, lines, output, *, mask=False):
    if not 0 < last - first <= PART_FRAMES:
        raise ValueError("A video note must contain between one frame and one minute.")
    subtitles = lines if settings.get("subtitles") in ("plain", "words") else []
    starts = [line["start"] for line in subtitles]
    duration = (last - first) / FPS
    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{SIZE}x{SIZE}", "-r", str(FPS), "-i", "pipe:0",
    ]
    if video.audio:
        command += ["-protocol_whitelist", "file,pipe", "-ss", str(first / FPS), "-t", str(duration), "-i", str(path),
                    "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "96k"]
    command += ["-t", str(duration), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-threads", "2", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL) as encoder:
        try:
            with video_frames(path, video.width, video.height, FPS, first, last) as frames:
                for number, frame in enumerate(frames, first):
                    canvas = frame_for(frame, number, settings, trajectory, subtitles, starts, mask)
                    encoder.stdin.write(canvas.tobytes())
            encoder.stdin.close()
            if encoder.wait(timeout=30):
                raise MediaError("Не удалось создать видеокружок.")
        except BrokenPipeError as error:
            raise MediaError("Не удалось создать видеокружок.") from error
        finally:
            if encoder.poll() is None:
                encoder.kill()
