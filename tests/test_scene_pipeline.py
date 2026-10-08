import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import scene_pipeline as sp  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FFMPEG_AVAILABLE = bool(FFMPEG and FFPROBE)

# Stand-ins for the two helper scripts, so the full run needs no network.
STUB_TTS = r'''
import argparse, os, shutil, sys
ap = argparse.ArgumentParser()
ap.add_argument("--voice")
ap.add_argument("--text-file", required=True)
ap.add_argument("--out", required=True)
args = ap.parse_args()
if os.environ.get("STUB_TTS_FAIL"):
    open(args.out, "wb").close()  # edge_tts_synth.py leaves an empty file behind on failure
    print("edge_tts_synth: synthesis failed: test", file=sys.stderr)
    sys.exit(1)
shutil.copyfile(os.environ["STUB_NARRATION"], args.out)
'''

STUB_WHISPER = r'''
import argparse, os
ap = argparse.ArgumentParser()
ap.add_argument("--model")
ap.add_argument("--audio", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--cache-dir", default="")
args = ap.parse_args()
with open(os.environ["STUB_WHISPER_LOG"], "a", encoding="utf-8") as log:
    log.write(args.audio + "\n")
first = "1\n00:00:00,000 --> 00:00:01,750\nEthan finds the old map in the attic.\n\n"
second = "2\n00:00:01,750 --> 00:00:03,700\nThen the storm knocks out the power.\n\n"
with open(args.out, "w", encoding="utf-8") as handle:
    handle.write(first if os.environ.get("STUB_WHISPER_SHORT") else first + second)
print("[whisper] done: 2 segments", flush=True)
'''

NARRATION_TEXT = "Ethan finds the old map in the attic. Then the storm knocks out the power.\n"


def _has_filter(name):
    proc = subprocess.run([FFMPEG, "-hide_banner", "-filters"], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name:
            return True
    return False


class ParseTests(unittest.TestCase):
    def test_parse_seconds_reads_ffprobe_output(self):
        self.assertEqual(sp.parse_seconds("3.700000\n", "x"), 3.7)

    def test_parse_seconds_rejects_missing_and_invalid_values(self):
        for bad in ("", "N/A", "nan", "inf", "-1", "0"):
            with self.subTest(value=bad), self.assertRaises(sp.SceneError):
                sp.parse_seconds(bad, "x")

    def test_frame_rate_parsing(self):
        self.assertAlmostEqual(sp._frame_rate("24/1"), 24.0)
        self.assertAlmostEqual(sp._frame_rate("30000/1001"), 29.97, places=2)
        self.assertIsNone(sp._frame_rate("0/0"))
        self.assertIsNone(sp._frame_rate(None))


class CommandTests(unittest.TestCase):
    def test_cut_trims_to_the_narration_length_and_drops_movie_audio(self):
        argv = sp.build_cut_command("ffmpeg", "/m/movie.mp4", 42.0, 3.7, "/w/clip.mp4")
        self.assertEqual(argv[argv.index("-t") + 1], "3.700")
        self.assertEqual(argv[argv.index("-ss") + 1], "42.000")
        self.assertLess(argv.index("-ss"), argv.index("-i"))  # input seeking
        self.assertIn("-an", argv)
        self.assertNotIn("copy", argv)  # stream copy would start on a keyframe

    def test_burn_in_maps_clip_video_and_narration_audio(self):
        argv = sp.build_burn_command("ffmpeg", "/w/clip.mp4", "/w/narration.mp3", "narration.srt",
                                     "fonts", "Inter", 3.7, "/w/final.mp4")
        maps = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-map"]
        self.assertEqual(maps, ["0:v:0", "1:a:0"])
        self.assertEqual(argv[argv.index("-t") + 1], "3.700")
        self.assertEqual(
            argv[argv.index("-vf") + 1],
            "subtitles=narration.srt:fontsdir=fonts:force_style="
            "'FontName=Inter,FontSize=15,Outline=1,Shadow=0,Alignment=2,MarginV=20'")

    def test_burn_in_without_a_font_uses_libass_defaults(self):
        argv = sp.build_burn_command("ffmpeg", "c.mp4", "n.mp3", "narration.srt", "", "", 3.7, "o.mp4")
        self.assertEqual(
            argv[argv.index("-vf") + 1],
            "subtitles=narration.srt:force_style='FontSize=15,Outline=1,Shadow=0,Alignment=2,MarginV=20'")

    def test_transcription_is_given_the_raw_narration_audio(self):
        argv = sp.build_transcribe_command("py", "whisper_transcribe.py", "small",
                                           "/w/narration.mp3", "/w/narration.srt", "")
        self.assertEqual(argv[argv.index("--audio") + 1], "/w/narration.mp3")
        self.assertNotIn("--cache-dir", argv)

    def test_font_names_that_would_break_the_filter_are_refused(self):
        with self.assertRaises(sp.SceneError):
            sp.check_font_name("Bad,Name")
        sp.check_font_name("Microsoft YaHei")  # spaces are fine inside force_style


class SubtitleTests(unittest.TestCase):
    def test_parse_srt_handles_bom_crlf_and_multiline_cues(self):
        raw = ("\ufeff1\r\n00:00:00,000 --> 00:00:01,750\r\nEthan finds\r\nthe old map.\r\n\r\n"
               "2\r\n00:00:01,750 --> 00:00:03,700\r\nThen the storm.\r\n")
        cues = sp.parse_srt(raw)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Ethan finds the old map.")
        self.assertAlmostEqual(cues[1].end, 3.7)

    def test_ellipsis_at_the_end_is_flagged(self):
        cues = [sp.Cue(0, 1.5, "Ethan finds the map."),
                sp.Cue(1.5, 3.7, "Then the storm knocks out the power...")]
        check = sp.check_subtitles(cues, NARRATION_TEXT)
        self.assertTrue(check.ends_with_ellipsis)
        self.assertTrue(check.tail_found)

    def test_a_finished_sentence_is_not_flagged(self):
        cues = [sp.Cue(0, 3.7, "Then the storm knocks out the power.")]
        check = sp.check_subtitles(cues, NARRATION_TEXT)
        self.assertFalse(check.ends_with_ellipsis)

    def test_truncated_final_sentence_is_detected(self):
        cues = [sp.Cue(0, 1.5, "Ethan finds the map."), sp.Cue(1.5, 3.0, "Then the storm")]
        check = sp.check_subtitles(cues, NARRATION_TEXT)
        self.assertFalse(check.tail_found)
        self.assertLess(check.coverage, 1.0)

    def test_extra_words_in_the_transcript_do_not_lower_coverage(self):
        cues = [sp.Cue(0, 2, "Ethan finds um the old map in the attic.")]
        check = sp.check_subtitles(cues, "Ethan finds the old map in the attic.")
        self.assertEqual(check.coverage, 1.0)

    def test_chinese_is_compared_character_by_character(self):
        text = "一切都从这里开始，风暴来了。"
        check = sp.check_subtitles([sp.Cue(0, 2, text)], text)
        self.assertTrue(check.tail_found)
        self.assertEqual(check.coverage, 1.0)


@unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg and ffprobe are needed for the media tests")
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="scene_test_")
        cls.narration = os.path.join(cls.tmp, "narration_source.mp3")
        cls.movie = os.path.join(cls.tmp, "movie.mp4")

        def ffmpeg(args):
            subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + args, check=True)

        # 3.7 s of MP3 in the same format edge-tts uses (mono, 24 kHz, 48 kbps).
        ffmpeg(["-f", "lavfi", "-i", "sine=frequency=300:sample_rate=24000:duration=3.7",
                "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k", cls.narration])
        ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=8",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", cls.movie])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _stubs(self, work):
        tts = os.path.join(work, "stub_tts.py")
        whisper = os.path.join(work, "stub_whisper.py")
        whisper_log = os.path.join(work, "whisper_calls.txt")
        with open(tts, "w", encoding="utf-8") as handle:
            handle.write(STUB_TTS)
        with open(whisper, "w", encoding="utf-8") as handle:
            handle.write(STUB_WHISPER)
        text_path = os.path.join(work, "scene.txt")
        with open(text_path, "w", encoding="utf-8") as handle:
            handle.write(NARRATION_TEXT)
        return tts, whisper, whisper_log, text_path

    def _config(self, work, tts, whisper, text_path, start=2.0):
        return sp.SceneConfig(
            movie=self.movie, start=start, text_file=text_path,
            workdir=os.path.join(work, "scene"), python=sys.executable,
            ffmpeg=FFMPEG, ffprobe=FFPROBE, tts_script=tts, whisper_script=whisper)

    def test_exact_length_ignores_mp3_padding(self):
        exact, _container = sp.measure_narration(FFMPEG, FFPROBE, self.narration, self.tmp)
        self.assertAlmostEqual(exact, 3.7, places=3)

    def test_full_run_with_stand_in_engines(self):
        if not _has_filter("subtitles"):
            self.skipTest("this ffmpeg build has no subtitles (libass) filter")
        with tempfile.TemporaryDirectory() as work:
            tts, whisper, whisper_log, text_path = self._stubs(work)
            cfg = self._config(work, tts, whisper, text_path)
            env = {"STUB_NARRATION": self.narration, "STUB_WHISPER_LOG": whisper_log}
            with patch.dict(os.environ, env):
                result = sp.run_pipeline(cfg, log=lambda _message: None)

            self.assertAlmostEqual(result.narration_seconds, 3.7, places=3)
            self.assertTrue(os.path.isfile(result.output))

            clip = sp.probe_streams(FFPROBE, os.path.join(work, "scene", "clip.mp4"))
            self.assertEqual(clip.audio_streams, 0)
            self.assertAlmostEqual(clip.video, 3.7, delta=1.0 / clip.fps + sp.ROUNDING)

            final = sp.probe_streams(FFPROBE, result.output)
            self.assertEqual((final.video_streams, final.audio_streams), (1, 1))
            self.assertAlmostEqual(final.audio, 3.7, delta=sp.AUDIO_TOLERANCE)
            self.assertAlmostEqual(final.video, 3.7, delta=1.0 / final.fps + sp.ROUNDING)

            with open(whisper_log, encoding="utf-8") as handle:
                audio_seen = handle.read().split()
            self.assertEqual(len(audio_seen), 1)
            self.assertTrue(audio_seen[0].endswith("narration.mp3"))
            self.assertEqual(result.subtitles.cue_count, 2)
            self.assertFalse(result.subtitles.ends_with_ellipsis)
            self.assertTrue(result.subtitles.tail_found)
            self.assertEqual(result.warnings, [])

    def test_failed_narration_leaves_no_empty_mp3_behind(self):
        with tempfile.TemporaryDirectory() as work:
            tts, whisper, whisper_log, text_path = self._stubs(work)
            cfg = self._config(work, tts, whisper, text_path)
            env = {"STUB_TTS_FAIL": "1", "STUB_NARRATION": self.narration, "STUB_WHISPER_LOG": whisper_log}
            with patch.dict(os.environ, env), self.assertRaises(sp.SceneError) as ctx:
                sp.run_pipeline(cfg, log=lambda _message: None)
            self.assertIn("edge_tts_synth.py failed", str(ctx.exception))
            self.assertFalse(os.path.exists(os.path.join(work, "scene", "narration.mp3")))

    def test_transcript_that_stops_early_is_reported_as_a_warning(self):
        with tempfile.TemporaryDirectory() as work:
            tts, whisper, whisper_log, text_path = self._stubs(work)
            cfg = self._config(work, tts, whisper, text_path)
            env = {"STUB_NARRATION": self.narration, "STUB_WHISPER_LOG": whisper_log,
                   "STUB_WHISPER_SHORT": "1"}
            with patch.dict(os.environ, env):
                result = sp.run_pipeline(cfg, log=lambda _message: None)
            self.assertFalse(result.subtitles.tail_found)
            self.assertTrue(any("final words" in warning for warning in result.warnings))
            self.assertTrue(any("ends 1.95 s before" in warning for warning in result.warnings))

    def test_start_past_the_end_of_the_movie_is_an_error(self):
        with tempfile.TemporaryDirectory() as work:
            tts, whisper, whisper_log, text_path = self._stubs(work)
            cfg = self._config(work, tts, whisper, text_path, start=60.0)
            env = {"STUB_NARRATION": self.narration, "STUB_WHISPER_LOG": whisper_log}
            with patch.dict(os.environ, env), self.assertRaises(sp.SceneError):
                sp.run_pipeline(cfg, log=lambda _message: None)


if __name__ == "__main__":
    unittest.main()
