#!/usr/bin/env python3
"""Local OpenAI-compatible proxy that humanizes the recap plan before DeepSeek returns it.

The bundled Windows executables contain their original prompts. Point
``openai_base_url`` at ``http://127.0.0.1:9200/v1`` and run this script in a
second terminal. The executable's Responses-API request receives a deliberate
404; its built-in OpenAI-compatible fallback then calls ``/chat/completions``,
which this proxy handles.

Passes: cast sheet (cached per movie + subtitle content), draft, polish, then
code-side timestamp/name validation and bounded name-repair retries.

Stdlib only; Python 3.8+.
"""
import hashlib
import html
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# The bundled Windows embeddable Python runs in isolated mode and may omit the
# script's directory from sys.path. Add it explicitly before importing our
# sibling prompt module.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from prompts import (
    CAST_SYSTEM,
    CAST_USER,
    DRAFT_SYSTEM,
    DRAFT_USER,
    POLISH_SYSTEM,
    POLISH_USER,
    REPAIR_SYSTEM,
    REPAIR_USER,
)

def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


UPSTREAM = os.environ.get("UPSTREAM", "https://api.deepseek.com").rstrip("/")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = _env_int("PORT", 9200, 1, 65535)
POLISH = os.environ.get("POLISH", "1") != "0"
CAST_TEMP = _env_float("CAST_TEMP", 0.3)
DRAFT_TEMP = _env_float("DRAFT_TEMP", 0.75)
POLISH_TEMP = _env_float("POLISH_TEMP", 0.7)
BATCH = _env_int("BATCH", 25, 1, 100)
NAME_RETRIES = _env_int("NAME_RETRIES", 2, 0, 5)
MAX_SRT_CHARS = _env_int("MAX_SRT_CHARS", 250000, 20000, 2_000_000)
MAX_SCRIPT_CHARS = _env_int("MAX_SCRIPT_CHARS", 120000, 10000, 500000)
MAX_STYLE_EXAMPLE_CHARS = _env_int("MAX_STYLE_EXAMPLE_CHARS", 18000, 2000, 50000)
MAX_BODY_BYTES = _env_int("MAX_BODY_BYTES", 20 * 1024 * 1024, 1024, 100 * 1024 * 1024)
SCRIPT_DIR = os.environ.get("SCRIPT_DIR", os.path.join(PROJECT_DIR, "scripts", "srt_files"))
CACHE = os.environ.get(
    "CAST_CACHE",
    os.path.join(PROJECT_DIR, "cast_cache"),
)
OUTRO_EN = (
    "If you enjoyed the video, don't forget to leave a like, subscribe, and turn on "
    "notifications. That's all for today. See you next time."
)

# The compiled app exposes these four recap languages. Keep every sign-off in
# the target language so the English CTA cannot turn a Chinese recap bilingual.
LANGUAGE_PROFILES = (
    {
        "keys": ("chinese", "mandarin", "中文", "zh"),
        "name": "Mandarin Chinese (Simplified characters)",
        "opening": "一切都从这里开始",
        "opening_separator": "，",
        "outro_separator": "",
        "outro": "如果你喜欢这期视频，别忘了点赞、订阅并开启通知。今天就到这里，我们下次再见。",
    },
    {
        "keys": ("arabic", "العربية", "ar"),
        "name": "Modern Standard Arabic",
        "opening": "تبدأ الحكاية",
        "opening_separator": "، ",
        "outro_separator": " ",
        "outro": "إذا أعجبكم الفيديو، فلا تنسوا الإعجاب به والاشتراك وتفعيل الإشعارات. هذا كل شيء لهذا اليوم. نراكم في المرة القادمة.",
    },
    {
        "keys": ("spanish", "español", "es"),
        "name": "Spanish (neutral Latin American)",
        "opening": "Todo comienza",
        "opening_separator": "... ",
        "outro_separator": " ",
        "outro": "Si te gustó el video, no olvides dejar un me gusta, suscribirte y activar las notificaciones. Eso es todo por hoy. Nos vemos la próxima vez.",
    },
    {
        "keys": ("english", "en"),
        "name": "English",
        "opening": "It all begins",
        "opening_separator": "... ",
        "outro_separator": " ",
        "outro": OUTRO_EN,
    },
)

COMMON_CAPITALIZED = {
    "a", "about", "after", "again", "all", "also", "an", "and", "another", "any", "are", "as", "at",
    "back", "because", "before", "being", "but", "by", "can", "could", "despite", "do", "during", "each",
    "even", "eventually", "every", "finally", "first", "for", "from", "he", "her", "here", "hers", "him",
    "his", "how", "however", "if", "in", "inside", "instead", "into", "it", "its", "just", "later", "less",
    "like", "meanwhile", "more", "most", "much", "must", "neither", "never", "next", "no", "nor", "not",
    "now", "of", "off", "on", "once", "one", "only", "or", "other", "our", "out", "over", "perhaps",
    "rather", "she", "since", "so", "some", "someone", "something", "soon", "still", "such", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "those", "though", "through", "to",
    "today", "too", "under", "until", "up", "upon", "very", "was", "we", "well", "were", "what", "whatever",
    "when", "whenever", "where", "whereas", "whether", "which", "while", "who", "whoever", "whom", "whose",
    "why", "will", "with", "within", "without", "would", "you", "your", "yeah", "nope", "okay", "alright",
    "pero", "luego", "mientras", "entonces", "así", "la", "el", "los", "las", "un", "una", "unos", "unas",
    "y", "de", "del", "en", "con", "por", "para", "cuando", "aunque", "ahora", "ella", "él", "ellos",
    "ellas", "su", "sus", "pero", "porque", "después", "antes", "finalmente", "sin", "sobre", "también",
    "i", "i'm", "i'll", "i've", "i'd", "god", "mr", "mrs", "ms", "dr", "christmas", "halloween", "mom",
    "dad", "mommy", "daddy", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december",
}

TIME_TOKEN = r"(?:\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?|\d{1,2}:\d{2}(?:[.,]\d+)?|\d+(?:\.\d+)?)"
RANGE_LINE_RE = re.compile(
    r"^\s*\[?\s*(?P<start>" + TIME_TOKEN + r")\s*(?:-->|[-–—])\s*"
    r"(?P<end>" + TIME_TOKEN + r")\s*\]?\s*(?P<text>.*)$"
)
WORD_RE = re.compile(r"[^\W\d_]+(?:[’'][^\W\d_]+)*", re.UNICODE)
LATIN_WORD_RE = re.compile(r"[A-Za-z\u00c0-\u024f]+(?:[’'][A-Za-z\u00c0-\u024f]+)*")
TAG_RE = re.compile(r"<[^>]{0,200}>")


def log(*parts: Any) -> None:
    print(time.strftime("[%H:%M:%S]"), *parts, flush=True)


class ProxyError(Exception):
    """A request/response error safe to return to the local executable."""


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(4096).decode("utf-8", "replace")
    except Exception:
        raw = ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            err = parsed.get("error", parsed)
            if isinstance(err, dict):
                raw = str(err.get("message") or err.get("code") or raw)
    except Exception:
        pass
    return raw[:1000] or exc.reason or "upstream request failed"


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces = []
        for part in value:
            if isinstance(part, dict):
                txt = part.get("text")
                if isinstance(txt, str):
                    pieces.append(txt)
        return "\n".join(pieces)
    return ""


def _post_json(url: str, body: Dict[str, Any], key: str, timeout: int = 900) -> Dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "moviecap-humanizer/1.0",
        },
        method="POST",
    )
    last_error: Optional[BaseException] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(30 * 1024 * 1024)
            parsed = json.loads(data.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ProxyError("upstream returned a non-object JSON response")
            return parsed
        except urllib.error.HTTPError as exc:
            message = _http_error_text(exc)
            log("upstream HTTP", exc.code, message)
            # Bad requests/auth/model errors cannot improve with retries.
            if exc.code in (400, 401, 403, 404, 405, 413, 422):
                raise ProxyError("upstream HTTP %d: %s" % (exc.code, message))
            last_error = ProxyError("upstream HTTP %d: %s" % (exc.code, message))
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError, ProxyError) as exc:
            last_error = exc
            log("upstream request attempt %d failed:" % (attempt + 1), str(exc))
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise ProxyError("upstream request failed after 3 attempts: %s" % (last_error or "unknown error"))


def chat(key: str, model: str, system: str, user: str, temperature: float,
         timeout: int = 900) -> str:
    """Call the configured OpenAI-compatible chat endpoint and return text content."""
    if not key:
        raise ProxyError("missing API key: enter a valid DeepSeek key in config.json or Settings")
    if not model:
        raise ProxyError("the executable sent an empty model name")
    body = {
        "model": model,
        "temperature": max(0.0, min(2.0, float(temperature))),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    result = _post_json(UPSTREAM + "/chat/completions", body, key, timeout=timeout)
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProxyError("upstream response did not contain choices[0]")
    message = choices[0].get("message") or {}
    content = _content_text(message.get("content"))
    if not content.strip():
        raise ProxyError("upstream returned an empty message")
    return content


def forward_chat_request(body: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Forward non-recap chat calls with their original JSON fields unchanged."""
    if not key:
        raise ProxyError("missing API key")
    return _post_json(UPSTREAM + "/chat/completions", body, key)


def parse_json(text: str) -> Dict[str, Any]:
    """Parse one JSON object, tolerating accidental markdown fences or preamble."""
    if not isinstance(text, str):
        raise ProxyError("model response was not text")
    cleaned = text.strip().lstrip("\ufeff")
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, count=1, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned, count=1)
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(cleaned[index:])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    raise ProxyError("model did not return a valid JSON object")


def _get_language(text: str) -> str:
    patterns = (
        r"LANGUAGE\s*:\s*Write\s+ALL\s+narrations\s+in\s+(.+?)\s+-\s+natural",
        r"TARGET\s+LANGUAGE\s*:\s*([^\r\n]+)",
        r"recap_language\s*[:=]\s*([^\r\n]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().strip(" '\"`")
            if candidate:
                return candidate
    return "English"


def _extract_rules(text: str) -> List[str]:
    wanted = (
        "each time range should",
        "keep each narration",
        "keep narrations punchy",
        "each narration should",
        "narration should",
        "target recap length",
    )
    rules = []
    for line in text.splitlines():
        normalized = line.strip().lstrip("-*• ").strip()
        if normalized and any(normalized.lower().startswith(prefix) for prefix in wanted):
            if normalized not in rules:
                rules.append(normalized)
    return rules


@dataclass
class BotPrompt:
    title: str
    srt: str
    n_clips: int
    language: str
    length_rules: List[str]
    script: str = ""


def clean_script_text(script: str, max_chars: Optional[int] = None) -> str:
    """Normalize an uploaded text source and bound its prompt size."""
    limit = max_chars or MAX_SCRIPT_CHARS
    text = html.unescape(script or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        keep = max(1, (limit - 100) // 2)
        text = (
            text[:keep].rstrip()
            + "\n\n[Middle of screenplay omitted to fit context; subtitles remain authoritative.]\n\n"
            + text[-keep:].lstrip()
        )
        log("WARNING: text context exceeded %d characters; retained beginning and ending" % limit)
    return text


def parse_bot_prompt(system: str, user: str) -> Optional[BotPrompt]:
    """Recognize the compiled executable's current Movie/INPUT A prompt safely."""
    combined = "\n".join(part for part in (system, user) if part)
    title_match = re.search(r"(?im)^\s*Movie:\s*(.+?)\s*$", combined)
    start_match = re.search(
        r"(?im)^\s*INPUT\s+A\s*\(Subtitles\s+with\s+timestamps\s+in\s+SECONDS\)\s*:\s*\n?",
        combined,
    )
    count_match = re.search(r"(?i)Choose\s+(\d+)\s+non-overlapping\s+time\s+ranges", combined)
    if not title_match or not start_match or not count_match:
        return None
    tail = combined[start_match.end():]
    end_match = re.search(
        r"(?im)^\s*INPUT\s+B\s*\(Optional\s+script\s+text\s+WITHOUT\s+timestamps;\s*may\s+be\s+empty\)\s*:",
        tail,
    )
    script_text = ""
    if not end_match:
        end_match = re.search(r"(?im)^\s*TASK\s*:", tail)
    elif tail[end_match.start():end_match.end()].lstrip().upper().startswith("INPUT B"):
        script_tail = tail[end_match.end():]
        task_match = re.search(r"(?im)^\s*TASK\s*:", script_tail)
        if task_match:
            script_text = script_tail[:task_match.start()].strip()
    if not end_match:
        return None
    source_srt = tail[:end_match.start()].strip()
    cues = parse_subtitle_cues(source_srt)
    if not cues:
        return None
    compacted = compact_srt(source_srt)
    if not compacted:
        return None
    return BotPrompt(
        title=title_match.group(1).strip().strip(" '\""),
        srt=compacted,
        n_clips=max(1, min(200, int(count_match.group(1)))),
        language=_get_language(combined),
        length_rules=_extract_rules(combined),
        script=clean_script_text(script_text),
    )


def load_uploaded_script(title: str) -> Tuple[str, str]:
    """Load a matching UTF-8 script uploaded beside the movie's SRT.

    Accepted convention: ``<Movie Title>.txt`` (preferred), with optional
    ``_script`` / `` script`` suffixes or Markdown extensions.
    """
    raw_stem = (title or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem = raw_stem
    for extension in (".mp4", ".mkv", ".avi", ".mov", ".m4v"):
        if stem.casefold().endswith(extension):
            stem = stem[:-len(extension)]
            break
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    if not stem:
        return "", ""
    candidates = (
        stem + ".txt",
        stem + "_script.txt",
        stem + " script.txt",
        stem + ".md",
        stem + "_script.md",
    )
    try:
        files = {name.casefold(): name for name in os.listdir(SCRIPT_DIR)}
    except OSError:
        return "", ""
    for candidate in candidates:
        actual_name = files.get(candidate.casefold())
        if not actual_name:
            continue
        path = os.path.join(SCRIPT_DIR, actual_name)
        try:
            if os.path.getsize(path) > 5 * 1024 * 1024:
                log("WARNING: uploaded screenplay is over 5 MB; ignoring", actual_name)
                continue
            with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
                text = clean_script_text(handle.read())
        except OSError as exc:
            log("WARNING: could not read uploaded screenplay", actual_name, exc)
            continue
        if text:
            log("loaded uploaded English screenplay:", actual_name, "(%d characters)" % len(text))
            return text, actual_name
    return "", ""


def select_script_context(title: str, embedded_script: str) -> Tuple[str, str]:
    """Prefer an uploaded screenplay; otherwise use the app's optional INPUT B."""
    uploaded, filename = load_uploaded_script(title)
    if uploaded:
        return uploaded, "uploaded file %s" % filename
    embedded = clean_script_text(embedded_script)
    if embedded:
        return embedded, "script embedded in app prompt"
    return "", "none (subtitles only)"


def load_style_example() -> Tuple[str, str]:
    """Read a global recap-style sample from scripts/srt_files without treating its plot as target content."""
    override = os.environ.get("STYLE_EXAMPLE_FILE", "").strip()
    if override:
        candidates = [override if os.path.isabs(override) else os.path.join(SCRIPT_DIR, override)]
    else:
        candidates = [
            os.path.join(SCRIPT_DIR, name)
            for name in ("recap_style_example.txt", "style_example.txt", "recap_example.txt", "recap_style_example.md")
        ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > 5 * 1024 * 1024:
                log("WARNING: style example is over 5 MB; ignoring", os.path.basename(path))
                continue
            with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
                text = clean_script_text(handle.read(), MAX_STYLE_EXAMPLE_CHARS)
        except OSError as exc:
            log("WARNING: could not read style example", os.path.basename(path), exc)
            continue
        if text:
            log("loaded recap style example:", os.path.basename(path), "(%d characters)" % len(text))
            return text, os.path.basename(path)
    return "", ""


def clean_subtitle_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(
        r"\[(?:music|applause|laughter|laughs|sighs|inaudible|silence|gasps|crosstalk|static)\]",
        " ", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"(?<!\S)(?:>{2,}|<{2,})(?!\S)", " ", text)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"\{\\(?:an\d+|pos\([^}]*\)|i\d*|b\d*|u\d*)\}", " ", text, flags=re.IGNORECASE)
    # Remove literal escaped line separators, subtitle speaker dashes and junk
    # without deleting the words that follow them.
    text = re.sub(r"\\[nN]\s*[-–—]+\s*", " ", text)
    text = re.sub(r"(?m)^\s*[-–—]{1,2}\s*", "", text)
    text = re.sub(r"(?<=\s)[-–—]{1,2}\s+", " ", text)
    text = text.replace("\x00", " ")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" \u200b\ufeff")
    return text


def parse_timepoint(value: str) -> Optional[float]:
    value = (value or "").strip().replace(",", ".")
    if not value:
        return None
    if ":" not in value:
        try:
            number = float(value)
            return number if math.isfinite(number) and number >= 0 else None
        except ValueError:
            return None
    try:
        parts = value.split(":")
        if len(parts) == 2:
            minutes, seconds = int(parts[0]), float(parts[1])
            number = minutes * 60 + seconds
        elif len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
            number = hours * 3600 + minutes * 60 + seconds
        else:
            return None
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


@dataclass
class Cue:
    start: float
    end: float
    text: str


def parse_subtitle_cues(srt: str) -> List[Cue]:
    """Read standard SRT, seconds-arrow text, or compact [start-end] text."""
    lines = (srt or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: List[Cue] = []
    index = 0
    while index < len(lines):
        match = RANGE_LINE_RE.match(lines[index])
        if not match:
            index += 1
            continue
        start = parse_timepoint(match.group("start"))
        end = parse_timepoint(match.group("end"))
        if start is None or end is None or end <= start:
            index += 1
            continue
        text_parts = [match.group("text").strip()] if match.group("text").strip() else []
        cursor = index + 1
        while cursor < len(lines):
            line = lines[cursor]
            if not line.strip():
                # A blank line may occur between continuation lines in a
                # malformed SRT; skip a single blank unless the next cue starts.
                look = cursor + 1
                while look < len(lines) and not lines[look].strip():
                    look += 1
                if look >= len(lines) or RANGE_LINE_RE.match(lines[look]):
                    cursor = look
                    break
                cursor += 1
                continue
            if RANGE_LINE_RE.match(line):
                break
            if line.strip().isdigit() and cursor + 1 < len(lines) and RANGE_LINE_RE.match(lines[cursor + 1]):
                break
            text_parts.append(line.strip())
            cursor += 1
        text = clean_subtitle_text(" ".join(text_parts))
        if text:
            cues.append(Cue(start, end, text))
        index = max(index + 1, cursor)
    cues.sort(key=lambda cue: (cue.start, cue.end))
    # Drop exact duplicate cues, common in merged subtitle files.
    unique: List[Cue] = []
    seen = set()
    for cue in cues:
        key = (round(cue.start, 3), round(cue.end, 3), cue.text.casefold())
        if key not in seen:
            seen.add(key)
            unique.append(cue)
    return unique


def format_seconds(value: float) -> str:
    rounded = round(float(value), 3)
    if abs(rounded - round(rounded)) < 0.0005:
        return str(int(round(rounded)))
    return ("%.3f" % rounded).rstrip("0").rstrip(".")


def _limit_cues(cues: Sequence[Cue], max_chars: int) -> List[Cue]:
    rendered = ["[%s-%s] %s" % (format_seconds(c.start), format_seconds(c.end), c.text) for c in cues]
    if sum(map(len, rendered)) <= max_chars:
        return list(cues)
    # Preserve coverage from the opening to the ending if an unusually large
    # subtitle file exceeds the model budget. Every selected item retains its
    # real timestamps; a warning is logged so omissions are visible.
    chosen: List[Cue] = []
    used = 0
    count = len(cues)
    target = max(2, min(count, int(max_chars / max(80, sum(map(len, rendered)) / max(1, count)))))
    indices = sorted(set(int(round(i * (count - 1) / max(1, target - 1))) for i in range(target)))
    for cue_index in indices:
        cue = cues[cue_index]
        line = "[%s-%s] %s" % (format_seconds(cue.start), format_seconds(cue.end), cue.text)
        remaining = max_chars - used
        if remaining <= 40:
            break
        if len(line) > remaining:
            room = max(1, remaining - len("[%s-%s] " % (format_seconds(cue.start), format_seconds(cue.end))))
            cue = Cue(cue.start, cue.end, cue.text[:room].rstrip())
            line = "[%s-%s] %s" % (format_seconds(cue.start), format_seconds(cue.end), cue.text)
        chosen.append(cue)
        used += len(line) + 1
    log("WARNING: compact subtitles exceeded %d characters; sampled %d of %d cues across the full runtime" %
        (max_chars, len(chosen), len(cues)))
    return chosen or list(cues[:1])


def compact_srt(srt: str, max_chars: Optional[int] = None) -> str:
    cues = parse_subtitle_cues(srt)
    if not cues:
        return ""
    cues = _limit_cues(cues, max_chars or MAX_SRT_CHARS)
    return "\n".join(
        "[%s-%s] %s" % (format_seconds(c.start), format_seconds(c.end), c.text)
        for c in cues
    )


def normalize_srt(srt: str, cast: Dict[str, Any]) -> str:
    """Normalize cast-sheet variants and subtitle markup before model calls."""
    cues = parse_subtitle_cues(srt)
    replacements: List[Tuple[str, str]] = []
    for character in cast.get("characters", []):
        canonical = str(character.get("name") or "").strip()
        if not canonical:
            continue
        variants = character.get("variants") or []
        if isinstance(variants, str):
            variants = [variants]
        for variant in variants:
            variant = str(variant or "").strip()
            if variant and variant.casefold() != canonical.casefold():
                replacements.append((variant, canonical))
    # Longer variants first prevents a short alias from partially consuming a
    # longer one. Latin names get token boundaries; CJK names are exact substrings.
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    fixed_cues = []
    for cue in cues:
        text = cue.text
        for variant, canonical in replacements:
            if re.search(r"[A-Za-z0-9_]", variant):
                pattern = r"(?<![A-Za-z0-9_])" + re.escape(variant) + r"(?![A-Za-z0-9_])"
                text = re.sub(pattern, lambda _m, value=canonical: value, text, flags=re.IGNORECASE)
            else:
                text = text.replace(variant, canonical)
        fixed_cues.append(Cue(cue.start, cue.end, clean_subtitle_text(text)))
    if not fixed_cues:
        return ""
    return "\n".join(
        "[%s-%s] %s" % (format_seconds(c.start), format_seconds(c.end), c.text)
        for c in fixed_cues if c.text
    )


def _cache_path(title: str, model: str, srt: str, script: str = "") -> str:
    digest = hashlib.sha256((model + "\0" + title + "\0" + srt + "\0" + script).encode("utf-8")).hexdigest()[:20]
    safe_title = re.sub(r"[^\w.-]+", "_", title, flags=re.UNICODE).strip("._")[:48] or "movie"
    return os.path.join(CACHE, "%s_%s.json" % (safe_title, digest))


_CACHE_LOCK = threading.Lock()


def _validate_cast(value: Dict[str, Any]) -> Dict[str, Any]:
    setting = str(value.get("setting") or "unclear").strip() or "unclear"
    characters = value.get("characters")
    if not isinstance(characters, list):
        characters = []
    cleaned = []
    seen = set()
    for item in characters:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        variants = item.get("variants") or []
        if isinstance(variants, str):
            variants = [variants]
        variants = [str(v).strip() for v in variants if str(v).strip()]
        mentions = item.get("mentions", 0)
        try:
            mentions = max(0, int(mentions))
        except (ValueError, TypeError, OverflowError):
            mentions = 0
        cleaned.append({
            "name": name,
            "variants": variants,
            "who": str(item.get("who") or "unclear").strip(),
            "relations": str(item.get("relations") or "unclear").strip(),
            "mentions": mentions,
        })
    return {"setting": setting, "characters": cleaned}


def get_cast(key: str, model: str, title: str, srt: str, script: str = "") -> Dict[str, Any]:
    os.makedirs(CACHE, exist_ok=True)
    path = _cache_path(title, model, srt, script)
    with _CACHE_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                cached = json.load(handle)
            if isinstance(cached, dict):
                valid = _validate_cast(cached)
                log("cast sheet cached for", title)
                return valid
        except (OSError, ValueError, TypeError):
            pass
        log("pass 0: building cast sheet for", title)
        cast_prompt = CAST_USER.format(
            title=title,
            srt=srt,
            script_context=script or "(No English screenplay provided; use subtitles only.)",
        )
        raw = parse_json(chat(key, model, CAST_SYSTEM, cast_prompt, CAST_TEMP))
        cast = _validate_cast(raw)
        # Atomic cache writes avoid a partially-written JSON file if the proxy
        # is interrupted while the model is responding.
        fd, temporary = tempfile.mkstemp(prefix="cast-", suffix=".tmp", dir=CACHE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(cast, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except OSError:
                pass
        log("cast sheet ready:", ", ".join(c["name"] for c in cast["characters"]) or "no named characters")
        return cast


def cast_text(cast: Dict[str, Any]) -> str:
    lines = []
    for character in cast.get("characters", []):
        name = character.get("name", "")
        variants = character.get("variants") or []
        variant_text = "; subtitle variants (do not use): " + ", ".join(variants) if variants else ""
        lines.append("- {name}{variants}: {who}. Relationship: {relations}.".format(
            name=name,
            variants=variant_text,
            who=character.get("who") or "unclear",
            relations=character.get("relations") or "unclear",
        ))
    return "\n".join(lines) if lines else "- No clearly named character is established; use neutral descriptions."


def language_profile(language: str) -> Dict[str, str]:
    value = (language or "English").strip().casefold()
    # Match actual language names or standalone ISO tags; substring matching
    # short tags (for example, "en" inside "French") can silently select English.
    def has_tag(tag: str) -> bool:
        return bool(re.search(r"(?<![a-z])" + re.escape(tag) + r"(?![a-z])", value))

    if "chinese" in value or "mandarin" in value or "中文" in value or has_tag("zh"):
        return LANGUAGE_PROFILES[0]
    if "arabic" in value or "العربية" in value or has_tag("ar"):
        return LANGUAGE_PROFILES[1]
    if "spanish" in value or "español" in value or has_tag("es"):
        return LANGUAGE_PROFILES[2]
    if "english" in value or has_tag("en"):
        return LANGUAGE_PROFILES[3]
    # Keep unknown selected languages instead of imposing an English opening
    # or CTA. The prompt asks the model to localize both naturally.
    return {"name": language or "English", "opening": "", "outro": ""}


def _word_units(text: str, language: str) -> int:
    # CJK text is normally written without spaces, so count Han/Kana/Hangul
    # characters rather than whitespace-separated tokens for the +/-15% check.
    if re.search(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]", text):
        return max(1, len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]", text)))
    return max(1, len(WORD_RE.findall(text)))


def _length_ok(original: str, rewritten: str, language: str) -> bool:
    before = _word_units(original, language)
    after = _word_units(rewritten, language)
    return before * 0.85 <= after <= before * 1.15


def _name_tokens(cast: Dict[str, Any]) -> Tuple[set, List[Tuple[str, str]]]:
    tokens = set()
    variants: List[Tuple[str, str]] = []
    for character in cast.get("characters", []):
        canonical = str(character.get("name") or "").strip()
        for token in LATIN_WORD_RE.findall(canonical):
            tokens.add(token.casefold())
        for variant in character.get("variants") or []:
            variant = str(variant).strip()
            if variant and variant.casefold() != canonical.casefold():
                variants.append((variant, canonical))
    return tokens, variants


def unknown_names(text: str, cast: Dict[str, Any], language: str = "English") -> List[str]:
    """Flag cast variants and likely Latin-script names for supported recap languages.

    Sentence-initial common words are ignored to avoid treating normal English,
    Spanish, or Arabic-transliteration grammar as a person's name. CJK/Arabic
    character names and unlisted target languages are constrained by the cast
    prompts instead of an unreliable capitalization heuristic.
    """
    allowed, variants = _name_tokens(cast)
    found = set()
    for variant, canonical in variants:
        if re.search(r"[A-Za-z0-9_]", variant):
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(variant) + r"(?![A-Za-z0-9_])"
            if re.search(pattern, text, flags=re.IGNORECASE):
                found.add("%s (use %s)" % (variant, canonical))
        elif variant and variant in text:
            found.add("%s (use %s)" % (variant, canonical))

    if language_profile(language).get("opening"):
        for sentence in re.split(r"(?<=[.!?。！？؟])\s+|\n+", text):
            for token in LATIN_WORD_RE.findall(sentence):
                if not token or not token[0].isupper() or token.isupper():
                    continue
                folded = token.casefold()
                if folded in allowed or folded in COMMON_CAPITALIZED:
                    continue
                found.add(token)
    return sorted(found, key=str.casefold)


def _canonicalize_variants(text: str, cast: Dict[str, Any]) -> str:
    replacements = []
    for character in cast.get("characters", []):
        canonical = str(character.get("name") or "").strip()
        for variant in character.get("variants") or []:
            variant = str(variant or "").strip()
            if variant and canonical and variant.casefold() != canonical.casefold():
                replacements.append((variant, canonical))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    for variant, canonical in replacements:
        if re.search(r"[A-Za-z0-9_]", variant):
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(variant) + r"(?![A-Za-z0-9_])"
            text = re.sub(pattern, lambda _m, value=canonical: value, text, flags=re.IGNORECASE)
        else:
            text = text.replace(variant, canonical)
    return text


def _subtitle_boundaries(srt: str) -> List[float]:
    cues = parse_subtitle_cues(srt)
    return sorted(set([cue.start for cue in cues] + [cue.end for cue in cues]))


def _nearest_boundary(value: float, boundaries: Sequence[float], tolerance: float = 2.0) -> Optional[float]:
    if not boundaries:
        return None
    closest = min(boundaries, key=lambda point: abs(point - value))
    return closest if abs(closest - value) <= tolerance else None


def clean_clips(raw_clips: Any, boundaries: Sequence[float]) -> List[Dict[str, Any]]:
    """Validate and snap clips to actual SRT cue boundaries; never fabricate time zero."""
    if not isinstance(raw_clips, list):
        return []
    candidates = []
    for raw in raw_clips:
        if not isinstance(raw, dict):
            continue
        try:
            start_value = float(raw["start"])
            end_value = float(raw["end"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if not math.isfinite(start_value) or not math.isfinite(end_value) or start_value <= 0:
            continue
        start = _nearest_boundary(start_value, boundaries)
        end = _nearest_boundary(end_value, boundaries)
        narration = clean_subtitle_text(str(raw.get("narration") or ""))
        if start is None or end is None or start <= 0 or end <= start or not narration:
            continue
        candidates.append((start, end, narration))
    candidates.sort(key=lambda item: (item[0], item[1]))
    result: List[Dict[str, Any]] = []
    last_end = -1.0
    for start, end, narration in candidates:
        if start < last_end:
            log("discarded overlapping clip at", format_seconds(start))
            continue
        result.append({"start": round(start, 3), "end": round(end, 3), "narration": narration})
        last_end = end
    return result


def _subtitle_window(srt: str, start: float, end: float) -> str:
    cues = [cue for cue in parse_subtitle_cues(srt) if cue.end >= start and cue.start <= end]
    return "\n".join(
        "[%s-%s] %s" % (format_seconds(c.start), format_seconds(c.end), c.text)
        for c in cues
    )


def _ensure_intro_outro(clips: List[Dict[str, Any]], profile: Dict[str, str]) -> None:
    if not clips:
        return
    opening = profile["opening"]
    first = clips[0]["narration"].lstrip()
    if opening and not first.casefold().startswith(opening.casefold()):
        separator = profile.get("opening_separator", " ")
        clips[0]["narration"] = opening + separator + first

    outro = profile["outro"]
    if not outro:
        return
    last = clips[-1]["narration"].rstrip()
    known_outros = [item["outro"] for item in LANGUAGE_PROFILES]
    for old_outro in known_outros:
        if last.endswith(old_outro):
            last = last[:-len(old_outro)].rstrip()
            break
    if not last.endswith(outro):
        separator = profile.get("outro_separator", " ")
        clips[-1]["narration"] = (last + separator + outro).strip()
    else:
        clips[-1]["narration"] = last


def _fix_notes_for(part: Sequence[Dict[str, Any]], cast: Dict[str, Any], language: str) -> str:
    notes = []
    for index, clip in enumerate(part, 1):
        issues = unknown_names(clip["narration"], cast, language)
        if issues:
            notes.append(
                "- Item %d contains unapproved names/spellings: %s. Replace each with the canonical CAST SHEET name or neutral wording."
                % (index, ", ".join(issues))
            )
    return "\nSPECIAL NAME FIXES:\n" + "\n".join(notes) if notes else ""


def _repair_name_issues(key: str, model: str, clips: List[Dict[str, Any]], cast: Dict[str, Any],
                        srt: str, language: str) -> None:
    ctext = cast_text(cast)
    for clip_index, clip in enumerate(clips):
        best = clip["narration"]
        best_issues = unknown_names(best, cast, language)
        if not best_issues:
            continue
        log("name check: clip %d has unapproved token(s): %s" % (clip_index + 1, ", ".join(best_issues)))
        evidence = _subtitle_window(srt, clip["start"], clip["end"])
        for attempt in range(NAME_RETRIES):
            try:
                prompt = REPAIR_USER.format(
                    language=language or "English",
                    cast=ctext,
                    subtitles=evidence or "(No subtitle cue matched this time window.)",
                    narration=best,
                    bad_names=", ".join(best_issues),
                )
                result = parse_json(chat(key, model, REPAIR_SYSTEM, prompt, min(POLISH_TEMP, 0.3)))
                candidate = _canonicalize_variants(str(result.get("narration") or "").strip(), cast)
                if not candidate:
                    continue
                candidate_issues = unknown_names(candidate, cast, language)
                if not candidate_issues:
                    best, best_issues = candidate, []
                    break
                if len(candidate_issues) < len(best_issues):
                    best, best_issues = candidate, candidate_issues
                log("name repair attempt %d for clip %d left: %s" %
                    (attempt + 1, clip_index + 1, ", ".join(candidate_issues)))
            except Exception as exc:
                log("name repair failed for clip %d (keeping best version): %s" % (clip_index + 1, exc))
                break
        clip["narration"] = best
        if best_issues:
            log("WARNING: clip %d still has unverified Latin-script proper-name candidate(s): %s" %
                (clip_index + 1, ", ".join(best_issues)))


def _polish_clips(key: str, model: str, clips: List[Dict[str, Any]], cast: Dict[str, Any],
                  language: str, srt: str) -> None:
    cast_summary = cast_text(cast)
    for start in range(0, len(clips), BATCH):
        part = clips[start:start + BATCH]
        original = [clip["narration"] for clip in part]
        item_blocks = []
        for index, (clip, narration) in enumerate(zip(part, original), 1):
            evidence = _subtitle_window(srt, clip["start"], clip["end"])
            item_blocks.append(
                "CLIP %d [%s-%s]\nDRAFT NARRATION: %s\nSUBTITLE CUES:\n%s" % (
                    start + index,
                    format_seconds(clip["start"]),
                    format_seconds(clip["end"]),
                    narration,
                    evidence or "(No subtitle cue overlaps this clip window.)",
                )
            )
        items = "\n\n".join(item_blocks)
        prompt = POLISH_USER.format(
            language=language or "English",
            cast=cast_summary,
            fix_notes=_fix_notes_for(part, cast, language),
            count=len(part),
            items=items,
        )
        try:
            result = parse_json(chat(key, model, POLISH_SYSTEM, prompt, POLISH_TEMP))
            new_texts = result.get("narrations")
            if not isinstance(new_texts, list) or len(new_texts) != len(part):
                log("polish batch size mismatch; retaining draft for that batch")
                continue
            for clip, before, after in zip(part, original, new_texts):
                after = _canonicalize_variants(str(after or "").strip(), cast)
                if after and _length_ok(before, after, language):
                    clip["narration"] = after
                elif after:
                    log("polish changed clip length by more than 15%; retaining its draft")
        except Exception as exc:
            log("polish batch failed; retaining draft:", exc)


def build_plan(key: str, model: str, title: str, source_srt: str, n_clips: int,
               length_rules: Sequence[str], language: str = "English",
               source_script: str = "", style_example: str = "") -> Dict[str, Any]:
    srt = compact_srt(source_srt)
    script = clean_script_text(source_script)
    style_sample = clean_script_text(style_example, MAX_STYLE_EXAMPLE_CHARS)
    cues = parse_subtitle_cues(srt)
    if not cues:
        raise ProxyError("could not parse any subtitle timestamps; request was not rewritten")
    cast = get_cast(key, model, title, srt, script)
    srt = normalize_srt(srt, cast)
    boundaries = _subtitle_boundaries(srt)
    if not boundaries:
        raise ProxyError("subtitle timestamps were unavailable after normalization")
    profile = language_profile(language)
    rules = list(length_rules) or [
        "Each narration should fit its selected time range at about 2.5 spoken words per second.",
        "Use concise story beats and keep clip durations within the original bot's requested ranges.",
    ]
    if profile["opening"] and profile["outro"]:
        opening_rule = "begin EXACTLY with the localized phrase %r" % profile["opening"]
        outro_rule = "The final narration must end EXACTLY with this localized sign-off:\n" + profile["outro"]
    else:
        opening_rule = (
            "begin with a short, idiomatic equivalent of 'The story begins' written only in %s; "
            "do not use the English phrase" % (language or "the selected language")
        )
        outro_rule = (
            "End the final narration with a short, natural story-ending and call to action localized "
            "only in %s. Invite comments, likes, and subscriptions if culturally appropriate; "
            "do not use the English sign-off" % (language or "the selected language")
        )
    draft_prompt = DRAFT_USER.format(
        title=title,
        language=language or "English",
        setting=cast.get("setting", "unclear"),
        cast=cast_text(cast),
        style_example=style_sample or "(No uploaded style example; follow the built-in narration rules.)",
        script_context=script or "(No screenplay supplied; rely on target subtitle windows only.)",
        n_clips=n_clips,
        opening_rule=opening_rule,
        outro_rule=outro_rule,
        length_rules="\n".join("- " + rule for rule in rules),
        srt=srt,
    )
    log("pass 1: drafting %d clips in %s ..." % (n_clips, language or "English"))
    draft_result = parse_json(chat(key, model, DRAFT_SYSTEM, draft_prompt, DRAFT_TEMP))
    clips = clean_clips(draft_result.get("clips"), boundaries)
    if not clips:
        raise ProxyError("draft returned no clips with valid subtitle timestamps")
    if len(clips) != n_clips:
        log("WARNING: requested %d clips; retained %d valid non-overlapping clips" % (n_clips, len(clips)))

    if POLISH:
        log("pass 2: polishing %d clips in batches of %d ..." % (len(clips), BATCH))
        _polish_clips(key, model, clips, cast, language, srt)
    _repair_name_issues(key, model, clips, cast, srt, language)
    _ensure_intro_outro(clips, profile)

    # This is the compiled executable's exact response contract. Strip any
    # incidental fields returned by the model and enforce finite numeric times.
    final = []
    for clip in clips:
        start, end = float(clip["start"]), float(clip["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start <= 0 or end <= start:
            continue
        final.append({"start": start, "end": end, "narration": str(clip["narration"]).strip()})
    if not final:
        raise ProxyError("all clips failed final validation")
    return {"clips": final}


def _message_texts(messages: Any) -> Tuple[str, str]:
    if not isinstance(messages, list):
        return "", ""
    systems, users = [], []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        text = _content_text(message.get("content"))
        if role == "system" or role == "developer":
            systems.append(text)
        elif role == "user":
            users.append(text)
    return "\n".join(systems), "\n".join(users)


def _api_key(handler: BaseHTTPRequestHandler) -> str:
    header = handler.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "MovieCapHumanizer/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid default access logs echoing query values. Never log request bodies
        # or Authorization headers (the latter carries the user's API key).
        log("http", self.address_string(), fmt % args)

    def _send(self, status: int, value: Dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": []})
        elif self.path in ("/", "/health", "/v1/health"):
            self._send(200, {"ok": True, "service": "moviecap-humanizer"})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if not self.path.split("?", 1)[0].rstrip("/").endswith("/chat/completions"):
            # The bundled exe first probes the Responses API, then uses this
            # explicit compatibility fallback for OpenAI-compatible services.
            self._send(404, {"error": {"message": "use /v1/chat/completions"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": {"message": "invalid Content-Length"}})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(413, {"error": {"message": "request body is empty or exceeds the configured size limit"}})
            return
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("request JSON must be an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._send(400, {"error": {"message": "invalid JSON request: %s" % exc}})
            return

        key = _api_key(self)
        model = str(body.get("model") or "deepseek-v4-pro")
        messages = body.get("messages", [])
        system, user = _message_texts(messages)
        context = parse_bot_prompt(system, user)
        try:
            if context is None:
                log("non-recap chat request -> forwarding unchanged")
                response = forward_chat_request(body, key)
            else:
                script, script_source = select_script_context(context.title, context.script)
                style_example, style_filename = load_style_example()
                style_source = style_filename or "none (built-in style rules only)"
                log("movie request: %r; %d clips; %d subtitle characters; target script=%d chars (%s); style example=%d chars (%s); language=%s" %
                    (context.title, context.n_clips, len(context.srt), len(script), script_source,
                     len(style_example), style_source, context.language))
                result = build_plan(
                    key=key,
                    model=model,
                    title=context.title,
                    source_srt=context.srt,
                    n_clips=context.n_clips,
                    length_rules=context.length_rules,
                    language=context.language,
                    source_script=script,
                    style_example=style_example,
                )
                response = {
                    "id": "chatcmpl-humanizer",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                        },
                    }],
                }
                log("completed recap plan:", len(result["clips"]), "clips")
            self._send(200, response)
        except ProxyError as exc:
            log("request failed:", exc)
            self._send(502, {"error": {"message": str(exc)}})
        except Exception as exc:
            log("unexpected request failure:", repr(exc))
            self._send(500, {"error": {"message": "humanizer failed: %s" % exc}})


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log("humanizer proxy listening at http://%s:%d/v1 -> %s (polish=%s, max_srt_chars=%d)" %
        (HOST, PORT, UPSTREAM, POLISH, MAX_SRT_CHARS))
    log("point config.json openai_base_url to http://%s:%d/v1 and run the Windows app" % (HOST, PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutdown requested")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
