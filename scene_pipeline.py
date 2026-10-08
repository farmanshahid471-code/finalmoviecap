#!/usr/bin/env python
"""Render one narrated movie scene, trimming the video to the narration.

    1. edge_tts_synth.py narrates the text into narration.mp3. ffprobe then
       measures its exact length (measure_narration explains why that needs a
       decode step).
    2. ffmpeg cuts the movie from --start for exactly that length -> clip.mp4.
       Video only and re-encoded: stream copy can only start on a keyframe.
    3. whisper_transcribe.py transcribes the raw narration.mp3, not the clip,
       so the subtitles follow the narration -> narration.srt.
    4. ffmpeg muxes the clip video with the uncut narration audio and burns
       narration.srt in with the subtitles filter -> final_scene.mp4.

Example, run from the project folder:

    python scene_pipeline.py --movie "movies/Sinners.mp4" --start 120.5 --text-file scene.txt

It needs ffmpeg and ffprobe with libx264, AAC and libass. Steps 1 and 3 run the
two helper scripts with --python, so that interpreter needs edge-tts and
faster-whisper (run.bat installs both into its private Python).
"""
import argparse
import difflib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
EDGE_TTS_SCRIPT = os.path.join(PROJECT_DIR, "edge_tts_synth.py")
WHISPER_SCRIPT = os.path.join(PROJECT_DIR, "whisper_transcribe.py")
DEFAULT_WORKDIR = os.path.join(PROJECT_DIR, "clips", "scene")
DEFAULT_CAPTION_FONT = os.path.join(PROJECT_DIR, "resources", "Inter-Regular.ttf")
DEFAULT_CAPTION_FONT_NAME = "Inter"

# libass scales FontSize with the frame height. Measured with Inter: FontSize 15 gives
# an em of 3.44 % of the frame height at 720p and 3.56 % at 1080p, the app's ~3.5 % caption.
CAPTION_STYLE = "FontSize=15,Outline=1,Shadow=0,Alignment=2,MarginV=20"

CLIP_CRF = "18"    # the clip is an intermediate, so keep it close to lossless
FINAL_CRF = "22"   # the same quality the app uses for its final encode
ROUNDING = 0.005   # seconds of container and timestamp rounding
AUDIO_TOLERANCE = 0.05
EARLY_END_SECONDS = 0.5  # a last subtitle this far before the audio end suggests dropped words

ELLIPSIS_AT_END = re.compile(r"(?:\.{2,}|\u2026)[\s\"'\u201d\u2019)\]]*$")
SRT_TIME = re.compile(r"\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")
TOKEN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]|[^\W_]+")
FILTER_UNSAFE = set("',:;\\[]")

Log = Callable[[str], None]


class SceneError(RuntimeError):
    """A step failed. The message names the step and what went wrong."""


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class SubtitleCheck:
    cue_count: int
    last: Cue
    ends_with_ellipsis: bool
    tail_found: bool   # the narration's final words end the transcript
    coverage: float    # share of narration words found in the transcript, in order


@dataclass
class StreamLengths:
    container: Optional[float]
    video: Optional[float]
    audio: Optional[float]
    fps: Optional[float]
    video_streams: int
    audio_streams: int


@dataclass
class SceneConfig:
    movie: str
    start: float
    text_file: str
    workdir: str = DEFAULT_WORKDIR
    out: str = ""
    voice: str = "en-US-GuyNeural"
    whisper_model: str = "small"
    whisper_cache: str = ""
    python: str = sys.executable
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    caption_font: str = DEFAULT_CAPTION_FONT
    caption_font_name: str = DEFAULT_CAPTION_FONT_NAME
    tts_script: str = EDGE_TTS_SCRIPT
    whisper_script: str = WHISPER_SCRIPT


@dataclass
class SceneResult:
    narration_seconds: float
    output: str
    subtitles: SubtitleCheck
    warnings: List[str]


def _log(message: str) -> None:
    print(message, flush=True)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _tail(text: str, lines: int = 6) -> str:
    kept = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " | ".join(kept[-lines:]) or "(no output)"


def _existing(path: str, what: str) -> str:
    if not path or not os.path.isfile(path):
        raise SceneError("%s not found: %s" % (what, path or "(no path given)"))
    return os.path.abspath(path)


def check_font_name(name: str) -> None:
    bad = sorted(set(name) & FILTER_UNSAFE)
    if bad:
        raise SceneError("the caption font name cannot contain %s" % " ".join(bad))


def run_tool(argv: Sequence[str], step: str, cwd: Optional[str] = None):
    try:
        return subprocess.run(
            list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        raise SceneError("%s: could not start %s (%s)" % (step, argv[0], exc))


def _run_ffmpeg(argv: Sequence[str], step: str, cwd: Optional[str] = None) -> None:
    proc = run_tool(argv, step, cwd=cwd)
    if proc.returncode != 0:
        raise SceneError("ffmpeg could not %s: %s" % (step, _tail(proc.stderr)))


# ----------------------------------------------------------------- probing

def parse_seconds(text: str, what: str) -> float:
    """Turn ffprobe's single-value output into a finite, positive number of seconds."""
    raw = (text or "").strip()
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value <= 0:
        raise SceneError("ffprobe gave no usable length for %s: %r" % (what, raw))
    return value


def probe_length(ffprobe: str, path: str) -> float:
    proc = run_tool([ffprobe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", path], "ffprobe")
    if proc.returncode != 0:
        raise SceneError("ffprobe could not read %s: %s" % (path, _tail(proc.stderr)))
    return parse_seconds(proc.stdout, path)


def _seconds_or_none(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _frame_rate(text) -> Optional[float]:
    num, _, den = str(text or "").partition("/")
    try:
        rate = float(num) / (float(den) if den else 1.0)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


def probe_streams(ffprobe: str, path: str) -> StreamLengths:
    proc = run_tool([ffprobe, "-v", "error", "-show_entries",
                     "format=duration:stream=codec_type,duration,r_frame_rate",
                     "-of", "json", path], "ffprobe")
    if proc.returncode != 0:
        raise SceneError("ffprobe could not read %s: %s" % (path, _tail(proc.stderr)))
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        raise SceneError("ffprobe returned unreadable output for %s" % path)
    streams = data.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    return StreamLengths(
        container=_seconds_or_none((data.get("format") or {}).get("duration")),
        video=_seconds_or_none(video[0].get("duration")) if video else None,
        audio=_seconds_or_none(audio[0].get("duration")) if audio else None,
        fps=_frame_rate(video[0].get("r_frame_rate")) if video else None,
        video_streams=len(video),
        audio_streams=len(audio),
    )


def measure_narration(ffmpeg: str, ffprobe: str, mp3: str, scratch_dir: str) -> Tuple[float, float]:
    """Return (exact_seconds, container_seconds) for the narration MP3.

    ffprobe's format duration for an MP3 covers whole MPEG frames, so it includes
    the encoder's start and end padding: a 3.700 s narration can report 3.768 s.
    Decoding to PCM first gives the length of the samples that actually play.
    """
    container = probe_length(ffprobe, mp3)
    pcm = os.path.join(scratch_dir, "_length_probe.wav")
    try:
        _run_ffmpeg([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", mp3,
                     "-vn", "-c:a", "pcm_s16le", pcm], "decode the narration to measure it")
        exact = probe_length(ffprobe, pcm)
    finally:
        _remove(pcm)
    return exact, container


def frame_tolerance(fps: Optional[float]) -> float:
    """One frame is the smallest step a video can take, so up to a frame of difference is normal."""
    return (1.0 / fps if fps else 0.05) + ROUNDING


def _check_length(actual: Optional[float], expected: float, tolerance: float, what: str) -> float:
    if actual is None:
        raise SceneError("could not read the length of the %s" % what)
    delta = actual - expected
    if abs(delta) > tolerance:
        raise SceneError("the %s is %.3f s but the narration is %.3f s (off by %+.3f s)"
                         % (what, actual, expected, delta))
    return delta


# --------------------------------------------------------- ffmpeg commands

def build_cut_command(ffmpeg: str, movie: str, start: float, duration: float, out_path: str) -> List[str]:
    """Video-only clip of exactly `duration` seconds starting at `start`.

    -ss before -i seeks accurately because the video is re-encoded. The scale
    filter only rounds odd frame sizes down to even ones, which libx264 needs;
    it does nothing to even sizes.
    """
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "%.3f" % start, "-i", movie,
            "-t", "%.3f" % duration,
            "-map", "0:v:0", "-an",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", CLIP_CRF, "-pix_fmt", "yuv420p",
            out_path]


def caption_style(font_name: str) -> str:
    return ("FontName=%s," % font_name if font_name else "") + CAPTION_STYLE


def build_burn_command(ffmpeg: str, clip: str, narration: str, srt_name: str, fonts_dir: str,
                       font_name: str, duration: float, out_path: str) -> List[str]:
    """Mux the clip video with the uncut narration audio and burn the subtitles in.

    Streams are mapped explicitly so the narration is the only audio. The
    subtitle path is relative, because the command runs inside the work folder
    and that avoids escaping Windows drive letters in the filter string.
    """
    parts = ["subtitles=%s" % srt_name]
    if fonts_dir:
        parts.append("fontsdir=%s" % fonts_dir)
    parts.append("force_style='%s'" % caption_style(font_name))
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", clip, "-i", narration,
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", ":".join(parts),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", FINAL_CRF, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-t", "%.3f" % duration,
            "-movflags", "+faststart",
            out_path]


def build_tts_command(python: str, tts_script: str, voice: str, text_file: str, out_mp3: str) -> List[str]:
    return [python, tts_script, "--voice", voice, "--text-file", text_file, "--out", out_mp3]


def build_transcribe_command(python: str, whisper_script: str, model: str, audio: str,
                             out_srt: str, cache_dir: str) -> List[str]:
    argv = [python, whisper_script, "--model", model, "--audio", audio, "--out", out_srt]
    if cache_dir:
        argv += ["--cache-dir", cache_dir]
    return argv


# ---------------------------------------------------------------- subtitles

def _srt_seconds(value: str) -> Optional[float]:
    match = SRT_TIME.match(value)
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            + int(fraction.ljust(3, "0")) / 1000.0)


def parse_srt(content: str) -> List[Cue]:
    text = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues: List[Cue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        for index, line in enumerate(lines):
            if "-->" not in line:
                continue
            left, _, right = line.partition("-->")
            start, end = _srt_seconds(left), _srt_seconds(right)
            body = " ".join(lines[index + 1:]).strip()
            if start is not None and end is not None and end > start and body:
                cues.append(Cue(start, end, body))
            break
    return cues


def tokens(text: str) -> List[str]:
    """Words for Latin-script text; single characters for CJK, which has no spaces."""
    return TOKEN.findall(text.casefold())


def _is_subsequence(needle: Sequence[str], haystack: Sequence[str]) -> bool:
    remaining = iter(haystack)
    return all(token in remaining for token in needle)


def check_subtitles(cues: Sequence[Cue], narration: str) -> SubtitleCheck:
    last = cues[-1]
    expected = tokens(narration)
    heard = tokens(" ".join(cue.text for cue in cues))
    tail = expected[-3:]
    tail_found = bool(tail) and _is_subsequence(tail, heard[-12:])
    matcher = difflib.SequenceMatcher(None, expected, heard, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    coverage = matched / len(expected) if expected else 0.0  # extra words in the transcript don't lower it
    return SubtitleCheck(
        cue_count=len(cues),
        last=last,
        ends_with_ellipsis=bool(ELLIPSIS_AT_END.search(last.text)),
        tail_found=tail_found,
        coverage=coverage,
    )


# ----------------------------------------------------------------- pipeline

def run_pipeline(cfg: SceneConfig, log: Log = _log) -> SceneResult:
    movie = _existing(cfg.movie, "movie")
    text_source = _existing(cfg.text_file, "text file")
    if not math.isfinite(cfg.start) or cfg.start < 0:
        raise SceneError("--start must be 0 or more seconds into the movie")
    caption_font = _existing(cfg.caption_font, "caption font") if cfg.caption_font else ""
    font_name = cfg.caption_font_name if caption_font else ""
    if font_name:
        check_font_name(font_name)
    with open(text_source, encoding="utf-8-sig") as handle:
        narration_text = handle.read().strip()
    if not narration_text:
        raise SceneError("the text file is empty: %s" % text_source)

    workdir = os.path.abspath(cfg.workdir)
    os.makedirs(workdir, exist_ok=True)
    out = os.path.abspath(cfg.out) if cfg.out else os.path.join(workdir, "final_scene.mp4")
    text_path = os.path.join(workdir, "narration.txt")
    mp3 = os.path.join(workdir, "narration.mp3")
    clip = os.path.join(workdir, "clip.mp4")
    srt_name = "narration.srt"
    srt = os.path.join(workdir, srt_name)
    fonts_name = "fonts"
    warnings: List[str] = []

    def warn(message: str) -> None:
        warnings.append(message)
        log("      WARN: " + message)

    # Remove the previous run's outputs so an old file can't pass for a new one.
    for stale in (text_path, mp3, clip, srt, out):
        _remove(stale)
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(narration_text + "\n")

    # 1. Narration, then its exact length.
    log("[1/4] Narration: edge_tts_synth.py -> narration.mp3")
    proc = run_tool(build_tts_command(cfg.python, cfg.tts_script, cfg.voice, text_path, mp3), "narration")
    if proc.returncode != 0 or _size(mp3) == 0:
        _remove(mp3)  # edge_tts_synth.py leaves an empty file behind when it fails
        raise SceneError("edge_tts_synth.py failed (exit %d): %s"
                         % (proc.returncode, _tail(proc.stderr or proc.stdout)))
    narration_seconds, container_seconds = measure_narration(cfg.ffmpeg, cfg.ffprobe, mp3, workdir)
    log("      ffprobe on the MP3 file:  %.6f s (includes MP3 encoder padding)" % container_seconds)
    log("      exact narration length:   %.6f s  <- used for every step below" % narration_seconds)

    # 2. Cut the movie to exactly that length.
    log("[2/4] Cut the movie from %.3f s for %.3f s (video only) -> clip.mp4"
        % (cfg.start, narration_seconds))
    try:
        _run_ffmpeg(build_cut_command(cfg.ffmpeg, movie, cfg.start, narration_seconds, clip),
                    "cut the clip")
        clip_info = probe_streams(cfg.ffprobe, clip)
        tolerance = frame_tolerance(clip_info.fps)
        delta = _check_length(clip_info.video, narration_seconds, tolerance, "clip video")
    except SceneError:
        _remove(clip)
        raise
    log("      clip video: %.6f s (%+.6f s vs narration; one frame is %.4f s) OK"
        % (clip_info.video, delta, 1.0 / (clip_info.fps or 25.0)))

    # 3. Transcribe the uncut narration (not the clip).
    log("[3/4] Transcribe the raw narration.mp3 (not the clip) -> narration.srt")
    proc = run_tool(build_transcribe_command(cfg.python, cfg.whisper_script, cfg.whisper_model,
                                             mp3, srt, cfg.whisper_cache), "transcription")
    if proc.returncode != 0 or not os.path.isfile(srt):
        _remove(srt)
        raise SceneError("whisper_transcribe.py failed (exit %d): %s"
                         % (proc.returncode, _tail(proc.stdout + "\n" + proc.stderr)))
    log("      " + _tail(proc.stdout, 1))
    with open(srt, encoding="utf-8-sig") as handle:
        cues = parse_srt(handle.read())
    if not cues:
        raise SceneError("Whisper found no speech in narration.mp3, so there are no subtitles")
    check = check_subtitles(cues, narration_text)
    last = check.last
    log("      %d cues. Last cue %.3f-%.3f s: \"%s\""
        % (check.cue_count, last.start, last.end, last.text))
    log("      narration words found in the transcript, in order: %d%%" % round(check.coverage * 100))
    if check.ends_with_ellipsis:
        warn("the last subtitle ends with an ellipsis")
    if not check.tail_found:
        warn("the narration's final words are not at the end of the subtitles")
    if last.end > narration_seconds + AUDIO_TOLERANCE:
        warn("the last subtitle runs past the end of the narration")
    elif narration_seconds - last.end > EARLY_END_SECONDS:
        warn("the last subtitle ends %.2f s before the narration does; final words may be missing"
             % (narration_seconds - last.end))

    # 4. Mux the clip with the uncut narration and burn in the subtitles.
    log("[4/4] Mux the clip video with the uncut narration and burn in narration.srt -> %s"
        % os.path.basename(out))
    fonts_dir = ""
    if caption_font:
        fonts_path = os.path.join(workdir, fonts_name)
        shutil.rmtree(fonts_path, ignore_errors=True)
        os.makedirs(fonts_path)
        shutil.copy2(caption_font, fonts_path)  # libass reads the font from this folder
        fonts_dir = fonts_name
    try:
        _run_ffmpeg(build_burn_command(cfg.ffmpeg, clip, mp3, srt_name, fonts_dir, font_name,
                                       narration_seconds, out),
                    "burn in the subtitles", cwd=workdir)
        final = probe_streams(cfg.ffprobe, out)
        if final.video_streams != 1 or final.audio_streams != 1:
            raise SceneError("the final file should have one video and one audio stream, found %d and %d"
                             % (final.video_streams, final.audio_streams))
        tolerance = frame_tolerance(final.fps)
        _check_length(final.video, narration_seconds, tolerance, "final video")
        _check_length(final.audio, narration_seconds, AUDIO_TOLERANCE, "final audio")
        _check_length(final.container, narration_seconds, tolerance, "final file")
    except SceneError:
        _remove(out)
        raise
    log("      final: video %.6f s | audio %.6f s | file %.6f s | narration %.6f s"
        % (final.video, final.audio, final.container, narration_seconds))
    log("Done: %s" % out)
    return SceneResult(narration_seconds=narration_seconds, output=out,
                       subtitles=check, warnings=warnings)


# ---------------------------------------------------------------------- CLI

def _tool(name: str, folder: str) -> str:
    if not folder:
        return name
    return os.path.join(folder, name + (".exe" if os.name == "nt" else ""))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Render one narrated movie scene: trim the video to the narration, "
                    "transcribe the narration and burn the subtitles in.")
    ap.add_argument("--movie", required=True, help="source movie file")
    ap.add_argument("--start", required=True, type=float,
                    help="scene start, in seconds into the movie")
    ap.add_argument("--text-file", required=True, help="UTF-8 text file with the narration")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR,
                    help="folder for the intermediate files (default: clips/scene)")
    ap.add_argument("--out", default="",
                    help="final video path (default: <workdir>/final_scene.mp4)")
    ap.add_argument("--voice", default="en-US-GuyNeural", help="Edge TTS voice")
    ap.add_argument("--whisper-model", default="small",
                    help="faster-whisper model: tiny, base, small, medium, large-v3")
    ap.add_argument("--whisper-cache", default=os.environ.get("HF_HOME", ""),
                    help="model cache folder (default: $HF_HOME)")
    ap.add_argument("--python", default=sys.executable,
                    help="Python with edge-tts and faster-whisper installed")
    ap.add_argument("--ffmpeg-dir", default="",
                    help="folder that holds ffmpeg and ffprobe (default: PATH)")
    ap.add_argument("--caption-font", default=DEFAULT_CAPTION_FONT,
                    help="caption font file; empty string uses libass's default font")
    ap.add_argument("--caption-font-name", default=DEFAULT_CAPTION_FONT_NAME,
                    help="font family name inside --caption-font "
                         "(for C:/Windows/Fonts/msyh.ttc use 'Microsoft YaHei')")
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    cfg = SceneConfig(
        movie=args.movie,
        start=args.start,
        text_file=args.text_file,
        workdir=args.workdir,
        out=args.out,
        voice=args.voice,
        whisper_model=args.whisper_model,
        whisper_cache=args.whisper_cache,
        python=args.python,
        ffmpeg=_tool("ffmpeg", args.ffmpeg_dir),
        ffprobe=_tool("ffprobe", args.ffmpeg_dir),
        caption_font=args.caption_font,
        caption_font_name=args.caption_font_name,
    )
    try:
        run_pipeline(cfg)
    except SceneError as exc:
        print("[ERROR] %s" % exc, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
