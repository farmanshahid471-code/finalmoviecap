import json
import tempfile
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import humanizer_proxy as proxy
from prompts import CAST_SYSTEM, DRAFT_SYSTEM, POLISH_SYSTEM, REPAIR_SYSTEM


class SubtitleTests(unittest.TestCase):
    def test_parses_compact_seconds_and_standard_srt(self):
        source = """1
00:00:01,250 --> 00:00:03,500
<i>Jessie</i> finds a key.

[3.5-7.25] Jesse opens the door.\n— Someone follows.
"""
        cues = proxy.parse_subtitle_cues(source)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 1.25)
        self.assertAlmostEqual(cues[0].end, 3.5)
        self.assertEqual(cues[0].text, "Jessie finds a key.")
        self.assertEqual(cues[1].text, "Jesse opens the door. Someone follows.")
        self.assertEqual(
            proxy.compact_srt(source),
            "[1.25-3.5] Jessie finds a key.\n[3.5-7.25] Jesse opens the door. Someone follows.",
        )
        self.assertEqual(proxy.parse_timepoint("01:23.5"), 83.5)
        self.assertEqual(
            proxy.clean_subtitle_text("&gt;&gt; [music] &gt;&gt; Jessie finds a key [laughter]"),
            "Jessie finds a key",
        )

    def test_compiled_prompt_parser_reads_language_from_system_and_user_from_user(self):
        system = (
            "LANGUAGE: Write ALL narrations in Mandarin Chinese (Simplified characters) "
            "- natural, fluent and native-sounding."
        )
        user = """Movie: Sample Movie

INPUT A (Subtitles with timestamps in SECONDS):
[0-5] Jesse finds a key.
[5-10] Jessie opens the door.

INPUT B (Optional script text WITHOUT timestamps; may be empty):
EXT. ISLAND - DAY
A cargo container washes ashore.

TASK:
- Choose 2 non-overlapping time ranges that best cover the full plot arc.
- Each time range should usually be 5-10 seconds long (end-start).
"""
        parsed = proxy.parse_bot_prompt(system, user)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.title, "Sample Movie")
        self.assertEqual(parsed.language, "Mandarin Chinese (Simplified characters)")
        self.assertEqual(parsed.n_clips, 2)
        self.assertIn("A cargo container washes ashore", parsed.script)
        self.assertEqual(len(proxy.parse_subtitle_cues(parsed.srt)), 2)
        self.assertIn("5-10 seconds", parsed.length_rules[0])

    def test_cast_variants_are_normalized_without_dropping_markup_text(self):
        cast = {"characters": [{"name": "Jessie", "variants": ["Jesse"]}]}
        source = "[1-2] <i>Jesse</i> says, let's go!"
        normalized = proxy.normalize_srt(source, cast)
        self.assertEqual(normalized, "[1-2] Jessie says, let's go!")


class ScriptContextTests(unittest.TestCase):
    def test_uploaded_script_precedes_scraped_prompt_context(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "Sample Movie.txt").write_text(
                "FADE IN:\nA cargo container reaches the island.", encoding="utf-8"
            )
            with patch.object(proxy, "SCRIPT_DIR", folder):
                script, source = proxy.select_script_context(
                    "Sample Movie", "Scraped draft from a different cut."
                )
        self.assertEqual(source, "uploaded file Sample Movie.txt")
        self.assertIn("cargo container", script)
        self.assertNotIn("different cut", script)

    def test_cast_cache_key_includes_screenplay_content(self):
        first = proxy._cache_path("Movie", "deepseek-v4-pro", "[1-2] Hello", "script A")
        second = proxy._cache_path("Movie", "deepseek-v4-pro", "[1-2] Hello", "script B")
        self.assertNotEqual(first, second)

    def test_global_style_example_is_loaded_separately_from_target_script(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "recap_style_example.txt").write_text(
                "A sample narrator uses brisk transitions. No plot facts should transfer.",
                encoding="utf-8",
            )
            with patch.object(proxy, "SCRIPT_DIR", folder):
                sample, filename = proxy.load_style_example()
        self.assertEqual(filename, "recap_style_example.txt")
        self.assertIn("brisk transitions", sample)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.cast = {
            "characters": [
                {"name": "Jessie", "variants": ["Jesse"]},
                {"name": "Buzz Lightyear", "variants": []},
            ]
        }

    def test_unknown_name_scan_flags_variant_and_unknown_proper_name(self):
        issues = proxy.unknown_names("Jessie meets Buzz, then Woody runs away.", self.cast)
        self.assertTrue(any("Woody" in item for item in issues))
        self.assertFalse(any("Jessie" in item for item in issues))
        self.assertFalse(any("Buzz" in item for item in issues))
        alias_issues = proxy.unknown_names("Jesse opens the door.", self.cast)
        self.assertTrue(any("Jesse" in item and "Jessie" in item for item in alias_issues))

    def test_clean_clips_rejects_zero_starts_invalid_times_and_overlap(self):
        boundaries = [0, 10, 15, 20, 30, 40]
        clips = proxy.clean_clips([
            {"start": 0, "end": 10, "narration": "The story begins."},
            {"start": 10.2, "end": 20.1, "narration": "A valid clip."},
            {"start": 15, "end": 30, "narration": "This overlaps."},
            {"start": 33, "end": 35, "narration": "Not close enough to real boundaries."},
            {"start": 30, "end": 40, "narration": "Last valid clip."},
        ], boundaries)
        self.assertEqual(len(clips), 2)
        self.assertEqual(clips[0]["start"], 10)
        self.assertEqual(clips[0]["end"], 20)
        self.assertEqual(clips[1]["start"], 30)
        self.assertGreater(clips[1]["start"], clips[0]["end"])

    def test_all_compiled_languages_have_localized_signoffs(self):
        cases = [
            ("English", "It all begins", "If you enjoyed the video"),
            ("Mandarin Chinese (Simplified characters)", "一切都从这里开始", "如果你喜欢这期视频"),
            ("Modern Standard Arabic", "تبدأ الحكاية", "إذا أعجبكم الفيديو"),
            ("Spanish (neutral Latin American)", "Todo comienza", "Si te gustó el video"),
        ]
        for language, opening, outro in cases:
            profile = proxy.language_profile(language)
            self.assertEqual(profile["opening"], opening)
            self.assertTrue(profile["outro"].startswith(outro))

    def test_localized_intro_outro_match_target_language(self):
        profile = proxy.language_profile("Mandarin Chinese (Simplified characters)")
        clips = [
            {"start": 10, "end": 20, "narration": "Jessie gets ready."},
            {"start": 20, "end": 30, "narration": "Jessie wins."},
        ]
        proxy._ensure_intro_outro(clips, profile)
        self.assertTrue(clips[0]["narration"].startswith("一切都从这里开始"))
        self.assertTrue(clips[-1]["narration"].endswith(profile["outro"]))
        self.assertNotIn(proxy.OUTRO_EN, clips[-1]["narration"])


class PipelineTests(unittest.TestCase):
    def test_cast_draft_polish_and_name_repair_return_bot_schema(self):
        srt = """[0-10] Jessie finds the old map.
[10-20] Jessie enters the room.
[20-30] Jessie meets a stranger.
[30-40] Jessie finds the way home.
"""
        profile = proxy.language_profile("Mandarin Chinese (Simplified characters)")
        first = "一切都从这里开始，Jessie发现一张旧地图。她想找到回家的路。"
        last = "Jessie找到了回家的路。Buzz给了她一个线索。" + profile["outro"]
        responses = [
            json.dumps({"setting": "unclear", "characters": [
                {"name": "Jessie", "variants": ["Jesse"], "who": "a character", "relations": "unclear", "mentions": 4}
            ]}, ensure_ascii=False),
            json.dumps({"clips": [
                {"start": 10, "end": 20, "narration": first},
                {"start": 20, "end": 40, "narration": last},
            ]}, ensure_ascii=False),
            json.dumps({"narrations": [first, last]}, ensure_ascii=False),
            json.dumps({"narration": "Jessie找到了回家的路。一个陌生人给了她一个线索。" + profile["outro"]}, ensure_ascii=False),
        ]
        seen_systems = []
        seen_users = []
        source_script = "FADE IN. Jessie discovers that the island changes everything."
        style_example = "A different movie's recap uses quick bridges and simple narration."

        def fake_chat(key, model, system, user, temperature, timeout=900):
            seen_systems.append(system)
            seen_users.append(user)
            if not responses:
                raise AssertionError("unexpected model call")
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as cache, patch.object(proxy, "CACHE", cache), patch.object(proxy, "chat", side_effect=fake_chat):
            result = proxy.build_plan(
                key="test-key",
                model="deepseek-test",
                title="Sample Movie",
                source_srt=srt,
                n_clips=2,
                length_rules=["Each clip should fit its timestamps."],
                language="Mandarin Chinese (Simplified characters)",
                source_script=source_script,
                style_example=style_example,
            )

        self.assertEqual(set(result.keys()), {"clips"})
        self.assertEqual(len(result["clips"]), 2)
        self.assertEqual(set(result["clips"][0].keys()), {"start", "end", "narration"})
        self.assertEqual([result["clips"][0]["start"], result["clips"][1]["start"]], [10, 20])
        self.assertTrue(result["clips"][0]["narration"].startswith("一切都从这里开始"))
        self.assertTrue(result["clips"][-1]["narration"].endswith(profile["outro"]))
        self.assertNotIn("Buzz", result["clips"][-1]["narration"])
        self.assertIn(CAST_SYSTEM, seen_systems)
        self.assertIn(DRAFT_SYSTEM, seen_systems)
        self.assertIn(POLISH_SYSTEM, seen_systems)
        self.assertIn(REPAIR_SYSTEM, seen_systems)
        self.assertIn(source_script, seen_users[0])
        self.assertIn(source_script, seen_users[1])
        self.assertNotIn(style_example, seen_users[0])
        self.assertIn(style_example, seen_users[1])
        self.assertIn("STYLE EXAMPLE ONLY", seen_users[1])
        self.assertIn("distinctive phrases", seen_users[1])
        self.assertIn("use its plot", seen_users[1])
        self.assertIn("TARGET STORY-TO-SUBTITLE ALIGNMENT", seen_users[1])
        self.assertIn("CLIPS, DRAFT NARRATIONS, AND TIME-ALIGNED SUBTITLE EVIDENCE", seen_users[2])
        self.assertIn("[10-20] Jessie enters the room.", seen_users[2])
        self.assertEqual(responses, [])

    def test_unlisted_language_is_not_forced_to_an_english_signoff(self):
        srt = "[1-10] Claire trouve une carte.\n[10-20] Elle rentre chez elle."
        narration = "Au début, Claire trouve une carte, puis elle rentre chez elle. Et voilà, l'histoire se termine."
        seen_draft = []

        def fake_chat(key, model, system, user, temperature, timeout=900):
            if system == CAST_SYSTEM:
                return json.dumps({"setting": "unclear", "characters": []})
            if system == DRAFT_SYSTEM:
                seen_draft.append(user)
                return json.dumps({"clips": [{"start": 1, "end": 20, "narration": narration}]}, ensure_ascii=False)
            if system == POLISH_SYSTEM:
                return json.dumps({"narrations": [narration]}, ensure_ascii=False)
            raise AssertionError("unexpected model call")

        with tempfile.TemporaryDirectory() as cache, patch.object(proxy, "CACHE", cache), patch.object(proxy, "chat", side_effect=fake_chat):
            result = proxy.build_plan(
                key="test-key", model="deepseek-test", title="Film", source_srt=srt,
                n_clips=1, length_rules=[], language="French",
            )

        self.assertEqual(proxy.language_profile("French")["opening"], "")
        self.assertIn("written only in French", seen_draft[0])
        self.assertIn("do not use the English sign-off", seen_draft[0])
        self.assertIn("REFERENCE STORYTELLING MODE", seen_draft[0])
        self.assertIn("one concrete story beat leads to the next", seen_draft[0])
        self.assertIn("transcript debris such as [music]", seen_draft[0])
        self.assertEqual(result["clips"][0]["narration"], narration)
        self.assertNotIn(proxy.OUTRO_EN, result["clips"][0]["narration"])


class HttpShimTests(unittest.TestCase):
    def test_health_responses_fallback_and_chat_forwarding(self):
        class StubUpstream(BaseHTTPRequestHandler):
            last_authorization = None
            last_body = None

            def log_message(self, *_args):
                pass

            def do_POST(self):
                type(self).last_authorization = self.headers.get("Authorization")
                raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                type(self).last_body = json.loads(raw_body.decode("utf-8"))
                response = json.dumps({
                    "id": "chatcmpl-stub",
                    "object": "chat.completion",
                    "model": "test-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "forwarded"}}],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), StubUpstream)
        app = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        app_thread = threading.Thread(target=app.serve_forever, daemon=True)
        upstream_thread.start()
        app_thread.start()
        try:
            with patch.object(proxy, "UPSTREAM", "http://127.0.0.1:%d" % upstream.server_port):
                root = "http://127.0.0.1:%d" % app.server_port
                with urllib.request.urlopen(root + "/health", timeout=2) as response:
                    self.assertEqual(json.loads(response.read())["ok"], True)

                payload = {
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "temperature": 0.25,
                    "stream": False,
                    "metadata": {"trace": "preserve-me"},
                }
                request = urllib.request.Request(
                    root + "/v1/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer test-secret"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    result = json.loads(response.read())
                self.assertEqual(result["choices"][0]["message"]["content"], "forwarded")
                self.assertEqual(StubUpstream.last_authorization, "Bearer test-secret")
                self.assertEqual(StubUpstream.last_body, payload)

                unsupported = urllib.request.Request(root + "/v1/responses", data=b"{}", method="POST")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(unsupported, timeout=2)
                self.assertEqual(error.exception.code, 404)
        finally:
            app.shutdown()
            upstream.shutdown()
            app.server_close()
            upstream.server_close()
            app_thread.join(timeout=2)
            upstream_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
