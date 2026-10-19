#!/usr/bin/env python3
"""Offline tests for the native host and the Linux installer."""

from __future__ import annotations

import json
import os
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ytdlp_host as host

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_EXTENSION_ID = "daaiinnmbkndbcooocjaegldpiclgfno"
WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def frames(blob: bytes) -> list[dict]:
    messages = []
    index = 0
    while index + 4 <= len(blob):
        length = struct.unpack_from("<I", blob, index)[0]
        index += 4
        messages.append(json.loads(blob[index:index + length]))
        index += length
    return messages


def run_host(messages: list[dict], wait_for: int | None = None) -> list[dict]:
    """Держит stdin открытым, пока не придут ответы, как это делает Chrome."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(host.__file__).resolve())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    for message in messages:
        data = json.dumps(message).encode("utf-8")
        proc.stdin.write(struct.pack("<I", len(data)) + data)
    proc.stdin.flush()
    needed = len(messages) if wait_for is None else wait_for
    chunks = b""
    deadline = time.time() + 20
    parsed: list[dict] = []
    while len(parsed) < needed and time.time() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            if proc.poll() is not None:
                break
            continue
        piece = os.read(proc.stdout.fileno(), 65536)
        if not piece:
            break
        chunks += piece
        parsed = frames(chunks)
    proc.stdin.close()
    deadline = time.time() + 20
    err = b""
    streams = {proc.stdout, proc.stderr}
    while streams and time.time() < deadline:
        ready, _, _ = select.select(list(streams), [], [], 0.2)
        if not ready:
            if proc.poll() is not None:
                break
            continue
        for stream in ready:
            piece = os.read(stream.fileno(), 65536)
            if not piece:
                streams.discard(stream)
                continue
            if stream is proc.stdout:
                chunks += piece
            else:
                err += piece
    code = proc.wait(timeout=20)
    proc.stdout.close()
    proc.stderr.close()
    if code != 0:
        raise AssertionError(err.decode("utf-8", "replace"))
    return frames(chunks)


class UrlAndFormatTests(unittest.TestCase):
    def test_accepts_watch_shorts_and_short_link(self):
        self.assertEqual(host.video_id_from_url(WATCH), "dQw4w9WgXcQ")
        self.assertEqual(
            host.video_id_from_url("https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(host.video_id_from_url("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(host.canonical_url("https://m.youtube.com/live/dQw4w9WgXcQ"), WATCH)

    def test_rejects_non_youtube_and_playlist_without_video(self):
        for url in (
            "https://example.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
            "javascript:alert(1)",
            "file:///tmp/video.mp4",
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/watch?v=short",
        ):
            with self.assertRaises(ValueError):
                host.video_id_from_url(url)

    def test_download_dir_stays_inside_home(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            resolved = host.resolve_download_dir(tmp)
            self.assertEqual(resolved, Path(tmp).resolve())
            self.assertTrue(resolved.is_dir())
        with self.assertRaises(ValueError):
            host.resolve_download_dir("Downloads/YouTube")
        with self.assertRaises(ValueError):
            host.resolve_download_dir("/tmp/outside-home")
        with self.assertRaises(ValueError):
            host.resolve_download_dir(str(Path.home()))

    def test_formats_follow_available_heights_and_pass_the_allowlist(self):
        info = {
            "formats": [
                {"vcodec": "avc1", "acodec": "none", "height": 1080, "ext": "mp4"},
                {"vcodec": "vp9", "acodec": "none", "height": 720, "ext": "webm"},
                {"vcodec": "none", "acodec": "mp4a", "height": None, "ext": "m4a"},
                {"vcodec": "none", "acodec": "opus", "ext": "webm"},
                {"vcodec": "none", "acodec": "none", "height": 1080, "ext": "mhtml"},
            ]
        }
        options = host.build_formats(info)
        labels = [option["label"] for option in options]
        self.assertEqual(
            labels,
            ["Лучшее качество", "1080p", "720p", "Только аудио (m4a)", "Только аудио (mp3)"],
        )
        self.assertNotIn("2160p", labels)
        for option in options:
            self.assertRegex(option["format"], host.FORMAT_RE)

    def test_progress_and_output_path_parsing(self):
        progress = host.parse_progress(
            "\x1b[34m[download]  45.2% of 10.00MiB at 1.20MiB/s ETA 00:09\x1b[0m"
        )
        self.assertEqual(progress["percent"], 45.2)
        self.assertEqual(progress["speed"], "1.20MiB/s")
        self.assertEqual(progress["eta"], "00:09")
        self.assertEqual(
            host.parse_progress('[Merger] Merging formats into "/tmp/a.mp4"')["status"],
            "Склеиваю видео и звук…",
        )
        state = {}
        host.note_output_path("[download] Destination: /tmp/video.f137.mp4", state)
        self.assertNotIn("final", state)
        host.note_output_path('[Merger] Merging formats into "/tmp/video.mp4"', state)
        self.assertEqual(state["final"], "/tmp/video.mp4")
        self.assertTrue(host.is_partial_path("/tmp/video.f140.m4a.part"))
        self.assertEqual(
            host.clean_ytdlp_error("WARNING: skip\nERROR: Sign in to confirm you’re not a bot"),
            "Sign in to confirm you’re not a bot",
        )


class ProcessTests(unittest.TestCase):
    def test_kill_process_group(self):
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        host.kill_process_group(proc)
        self.assertIsNotNone(proc.poll())

    def test_second_download_is_rejected(self):
        sent = []
        original = host.send
        host.send = sent.append
        host.download_active = True
        try:
            host.begin_download({"id": "busy", "action": "download", "url": WATCH, "format": "best"})
        finally:
            host.send = original
            host.download_active = False
        self.assertEqual(sent[0]["error"], "Уже идёт скачивание")

    def test_host_ping_and_rejects_foreign_url_without_network(self):
        messages = run_host([
            {"id": "ping-1", "action": "ping"},
            {"id": "info-1", "action": "info", "url": "https://example.com/watch?v=dQw4w9WgXcQ"},
            {
                "id": "dl-1",
                "action": "download",
                "url": WATCH,
                "format": "'; rm -rf /",
                "downloadDir": "",
            },
        ])
        by_id = {message["id"]: message for message in messages}
        self.assertTrue(by_id["ping-1"]["ok"])
        self.assertIn("ytdlp", by_id["ping-1"])
        self.assertIn("ffmpeg", by_id["ping-1"])
        self.assertFalse(by_id["info-1"]["ok"])
        self.assertIn("YouTube", by_id["info-1"]["error"])
        self.assertFalse(by_id["dl-1"]["ok"])
        self.assertIn("формат", by_id["dl-1"]["error"])

    def test_download_reports_missing_ffmpeg_without_network(self):
        if shutil.which("ffmpeg"):
            self.skipTest("ffmpeg установлен, проверка пропускается чтобы не скачивать ролик")
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            messages = run_host([{
                "id": "dl-ffmpeg",
                "action": "download",
                "url": WATCH,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "merge": True,
                "downloadDir": tmp,
            }])
        self.assertEqual(len(messages), 1)
        self.assertFalse(messages[0]["ok"])
        self.assertIn("ffmpeg", messages[0]["error"])


class InstallTests(unittest.TestCase):
    def test_extension_id_is_stable(self):
        manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        import base64
        import hashlib
        digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
        ext_id = "".join(chr(ord("a") + int(char, 16)) for char in digest)
        self.assertEqual(ext_id, EXPECTED_EXTENSION_ID)
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(sorted(manifest["permissions"]), ["nativeMessaging", "storage"])
        for name in ("background.js", "content.js", "content.css", "popup.html", "popup.js", "popup.css"):
            self.assertTrue((ROOT / "extension" / name).is_file(), name)

    def test_install_script_writes_host_manifests(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = Path(tmp)
            (home / ".config" / "chromium").mkdir(parents=True)
            env = os.environ.copy()
            env["HOME"] = str(home)
            proc = subprocess.run(
                ["bash", str(ROOT / "native-host" / "install.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            chrome = home / ".config" / "google-chrome" / "NativeMessagingHosts" / "com.alena.ytdlp.json"
            chromium = home / ".config" / "chromium" / "NativeMessagingHosts" / "com.alena.ytdlp.json"
            edge = home / ".config" / "microsoft-edge" / "NativeMessagingHosts" / "com.alena.ytdlp.json"
            self.assertTrue(chrome.is_file())
            self.assertTrue(chromium.is_file())
            self.assertFalse(edge.exists())
            gost = home / ".config" / "chromium-gost" / "NativeMessagingHosts" / "com.alena.ytdlp.json"
            if Path("/usr/bin/chromium-gost").is_file():
                self.assertTrue(gost.is_file())
            else:
                self.assertFalse(gost.exists())
            manifest = json.loads(chrome.read_text(encoding="utf-8"))
            self.assertEqual(manifest["path"], str(ROOT / "native-host" / "ytdlp_host.py"))
            self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{EXPECTED_EXTENSION_ID}/"])
            self.assertIn(EXPECTED_EXTENSION_ID, proc.stdout)
            if shutil.which("ffmpeg") is None:
                self.assertIn("Не найден ffmpeg", proc.stdout)
            if shutil.which("yt-dlp") is None:
                self.assertIn("Не найден yt-dlp", proc.stdout)


if __name__ == "__main__":
    unittest.main()
