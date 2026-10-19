#!/usr/bin/env python3
"""Native messaging host: Chrome extension <-> yt-dlp."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_MESSAGE = 1024 * 1024
HOST_NAME = "com.alena.ytdlp"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
FORMAT_RE = re.compile(r"^[A-Za-z0-9_\[\]<>=+./,^\-]+$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PARTIAL_RE = re.compile(r"\.f\d+\.|\.part$|\.ytdl$|\.temp\.")
PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%"
    r"(?:.*?at\s+(?P<speed>\S+))?"
    r"(?:\s+ETA\s+(?P<eta>\S+))?"
)
MERGE_RE = re.compile(r'^\[Merger\] Merging formats into "(?P<path>.+)"$')
EXTRACT_RE = re.compile(r"^\[ExtractAudio\] Destination: (?P<path>.+)$")
ALREADY_RE = re.compile(r"^\[download\] (?P<path>.+) has already been downloaded$")
DEST_RE = re.compile(r"^\[download\] Destination: (?P<path>.+)$")

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
PREFERRED_HEIGHTS = (2160, 1440, 1080, 720, 480, 360)
INSTALL_YTDLP = "mkdir -p ~/.local/bin && curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o ~/.local/bin/yt-dlp && chmod +x ~/.local/bin/yt-dlp"
INSTALL_FFMPEG = "sudo apt install ffmpeg"

send_lock = threading.Lock()
proc_lock = threading.Lock()
current_proc: subprocess.Popen | None = None
download_active = False
cancel_event = threading.Event()
workers: list[threading.Thread] = []


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    length = struct.unpack("<I", raw)[0]
    if length > MAX_MESSAGE:
        raise ValueError("Сообщение слишком большое")
    data = sys.stdin.buffer.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode("utf-8"))


def send(payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        data = json.dumps(
            {"id": payload.get("id"), "ok": False, "type": "error", "error": "Ответ слишком большой"},
            ensure_ascii=False,
        ).encode("utf-8")
    with send_lock:
        sys.stdout.buffer.write(struct.pack("<I", len(data)))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def video_id_from_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Нужна ссылка на ролик YouTube")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Нужна ссылка http или https на YouTube")
    host = parsed.hostname.lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError("Можно скачивать только ссылки YouTube")
    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        video_id = ""
        if parts and parts[0] in ("shorts", "live", "embed", "v") and len(parts) > 1:
            video_id = parts[1]
        elif parsed.path == "/watch" or parsed.path.startswith("/watch/"):
            values = parse_qs(parsed.query).get("v") or []
            video_id = values[0] if values else ""
    if not VIDEO_ID_RE.match(video_id):
        raise ValueError("Откройте страницу ролика или Shorts")
    return video_id


def canonical_url(url: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id_from_url(url)}"


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "YouTube"


def resolve_download_dir(raw: str | None) -> Path:
    if raw is None or not str(raw).strip():
        path = default_download_dir()
    else:
        text = str(raw).strip()
        if "\x00" in text:
            raise ValueError("Некорректный путь")
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ValueError("Укажите полный путь к папке")
    path = path.resolve()
    home = Path.home().resolve()
    if path == Path("/") or path == home:
        raise ValueError("Выберите папку внутри домашнего каталога, а не сам каталог и не корень диска")
    try:
        path.relative_to(home)
    except ValueError as exc:
        raise ValueError("Папка должна быть внутри домашнего каталога") from exc
    if path.exists() and not path.is_dir():
        raise ValueError("Путь указывает на файл, нужна папка")
    path.mkdir(parents=True, exist_ok=True)
    return path


def candidate_binaries(name: str) -> list[str]:
    found: list[str] = []
    discovered = shutil.which(name)
    if discovered:
        found.append(discovered)
    for directory in (Path.home() / ".local" / "bin", Path("/usr/local/bin")):
        path = directory / name
        if path.is_file() and os.access(path, os.X_OK) and str(path) not in found:
            found.append(str(path))
    return found


def newest_binary(name: str, args: list[str], pattern: str | None = None) -> tuple[str | None, str | None]:
    best_path = None
    best_version = None
    for path in candidate_binaries(name):
        version = command_version(path, args, pattern)
        if best_path is None or (version or "") > (best_version or ""):
            best_path = path
            best_version = version
    return best_path, best_version


def command_version(path: str, args: list[str], pattern: str | None = None) -> str | None:
    try:
        output = subprocess.check_output(
            [path, *args],
            stderr=subprocess.STDOUT,
            timeout=10,
            text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    line = output.strip().splitlines()[0] if output.strip() else ""
    if pattern:
        match = re.search(pattern, line)
        return match.group(1) if match else line[:80]
    return line[:80]


def tool_status() -> dict:
    ytdlp, ytdlp_version = newest_binary("yt-dlp", ["--version"])
    ffmpeg, ffmpeg_version = newest_binary("ffmpeg", ["-version"], r"ffmpeg version (\S+)")
    missing = []
    if not ytdlp:
        missing.append("yt-dlp")
    if not ffmpeg:
        missing.append("ffmpeg")
    hints = []
    if "yt-dlp" in missing:
        hints.append(INSTALL_YTDLP)
    if "ffmpeg" in missing:
        hints.append(INSTALL_FFMPEG)
    return {
        "ytdlp": bool(ytdlp),
        "ffmpeg": bool(ffmpeg),
        "ytdlpPath": ytdlp,
        "ffmpegPath": ffmpeg,
        "ytdlpVersion": ytdlp_version,
        "ffmpegVersion": ffmpeg_version,
        "downloadDir": str(default_download_dir()),
        "ready": not missing,
        "hint": ("Не найдено: " + ", ".join(missing) + ". Установите: " + "; ".join(hints)) if missing else "",
    }


def require_tools() -> dict:
    status = tool_status()
    if not status["ready"]:
        raise RuntimeError(status["hint"])
    return status


def validate_format(fmt: object) -> str:
    if not isinstance(fmt, str) or not FORMAT_RE.match(fmt) or len(fmt) > 400:
        raise ValueError("Неизвестный формат")
    return fmt


def build_formats(info: dict) -> list[dict]:
    heights = set()
    has_m4a_audio = False
    for fmt in info.get("formats") or []:
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"
        height = fmt.get("height")
        if vcodec != "none" and isinstance(height, int) and height > 0:
            heights.add(height)
        if acodec != "none" and vcodec == "none" and (fmt.get("ext") or "").lower() == "m4a":
            has_m4a_audio = True

    options = [{
        "id": "best",
        "label": "Лучшее качество",
        "kind": "video",
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge": True,
    }]
    for height in PREFERRED_HEIGHTS:
        if height not in heights:
            continue
        options.append({
            "id": f"h{height}",
            "label": f"{height}p",
            "kind": "video",
            "height": height,
            "format": (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}]"
            ),
            "merge": True,
        })
    options.append({
        "id": "audio-m4a",
        "label": "Только аудио (m4a)" if has_m4a_audio else "Только аудио",
        "kind": "audio",
        "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio",
        "merge": False,
    })
    options.append({
        "id": "audio-mp3",
        "label": "Только аудио (mp3)",
        "kind": "audio",
        "format": "bestaudio/best",
        "merge": False,
        "extractAudio": "mp3",
    })
    return options


def clean_ytdlp_error(text: str) -> str:
    lines = []
    for raw in strip_ansi(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("WARNING:"):
            continue
        lines.append(line)
    if not lines:
        return "Не удалось скачать ролик"
    errors = [line for line in lines if line.startswith("ERROR:")]
    chosen = (errors[-1] if errors else lines[-1]).removeprefix("ERROR:").strip()
    if len(chosen) > 400:
        return chosen[:400] + "…"
    return chosen


def parse_progress(line: str) -> dict | None:
    text = strip_ansi(line).strip()
    if "[Merger]" in text:
        return {"status": "Склеиваю видео и звук…", "percent": None, "speed": "", "eta": ""}
    if "[ExtractAudio]" in text:
        return {"status": "Сохраняю аудио…", "percent": None, "speed": "", "eta": ""}
    match = PROGRESS_RE.search(text)
    if not match:
        return None
    return {
        "status": "Скачиваю…",
        "percent": float(match.group("pct")),
        "speed": match.group("speed") or "",
        "eta": match.group("eta") or "",
    }


def is_partial_path(path: str) -> bool:
    return bool(PARTIAL_RE.search(Path(path).name))


def note_output_path(line: str, state: dict) -> None:
    text = strip_ansi(line).strip()
    for regex, final in ((MERGE_RE, True), (EXTRACT_RE, True), (ALREADY_RE, True), (DEST_RE, False)):
        match = regex.search(text)
        if not match:
            continue
        path = match.group("path").strip()
        if final or not is_partial_path(path):
            state["final"] = path
        return


def newest_file(directory: Path, started: float) -> str | None:
    best = None
    best_mtime = started - 1
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file() or is_partial_path(entry.name):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= started - 1 and mtime >= best_mtime:
            best = entry
            best_mtime = mtime
    return str(best) if best else None


def kill_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except (PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def cancel_download() -> None:
    cancel_event.set()
    with proc_lock:
        proc = current_proc
    if proc is not None:
        kill_process_group(proc)


def fetch_info(url: str) -> dict:
    status = tool_status()
    if not status["ytdlp"]:
        raise RuntimeError(status["hint"])
    proc = subprocess.run(
        [status["ytdlpPath"], "-J", "--no-playlist", "--no-progress", url],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if proc.returncode != 0:
        raise RuntimeError(clean_ytdlp_error(proc.stderr or proc.stdout or "yt-dlp завершился с ошибкой"))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp вернул не JSON") from exc
    duration = data.get("duration")
    return {
        "title": data.get("title") or "Видео",
        "channel": data.get("channel") or data.get("uploader") or "",
        "duration": int(duration) if isinstance(duration, (int, float)) else None,
        "videoId": data.get("id") or "",
        "formats": build_formats(data),
    }


def pump_stderr(pipe, lines: list[str]) -> None:
    try:
        for raw in iter(lambda: pipe.readline(), b""):
            text = strip_ansi(raw.decode("utf-8", "replace")).rstrip()
            if not text:
                continue
            lines.append(text)
            if len(lines) > 50:
                del lines[:-50]
    except OSError:
        return


def run_download(message: dict) -> None:
    global current_proc, download_active
    request_id = message.get("id")
    proc = None
    try:
        if cancel_event.is_set():
            send({"id": request_id, "ok": False, "type": "cancelled", "error": "Скачивание отменено"})
            return
        url = canonical_url(message.get("url") or "")
        fmt = validate_format(message.get("format"))
        directory = resolve_download_dir(message.get("downloadDir"))
        status = require_tools()
        if cancel_event.is_set():
            send({"id": request_id, "ok": False, "type": "cancelled", "error": "Скачивание отменено"})
            return
        outtmpl = str(directory / "%(title).100s [%(id)s].%(ext)s")
        command = [
            status["ytdlpPath"],
            "--no-playlist",
            "--newline",
            "-o",
            outtmpl,
            "-f",
            fmt,
        ]
        if message.get("merge"):
            command.extend(["--merge-output-format", "mp4"])
        extract = message.get("extractAudio")
        if extract:
            if extract not in ("mp3", "m4a"):
                raise ValueError("Неизвестный аудиоформат")
            command.extend(["-x", "--audio-format", extract])
        command.append(url)
        started = time.time()
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        with proc_lock:
            current_proc = proc
        paths = {"final": None}
        stderr_lines: list[str] = []
        threading.Thread(target=pump_stderr, args=(proc.stderr, stderr_lines), daemon=True).start()
        lines: queue.Queue = queue.Queue()

        def pump_stdout() -> None:
            try:
                for raw in iter(lambda: proc.stdout.readline(), b""):
                    lines.put(raw)
            finally:
                lines.put(None)

        threading.Thread(target=pump_stdout, daemon=True).start()
        last_progress = 0.0
        while True:
            if cancel_event.is_set():
                kill_process_group(proc)
                break
            try:
                raw = lines.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None and lines.empty():
                    break
                continue
            if raw is None:
                break
            text = raw.decode("utf-8", "replace")
            note_output_path(text, paths)
            progress = parse_progress(text)
            if not progress:
                continue
            now = time.monotonic()
            percent = progress.get("percent")
            if isinstance(percent, float) and percent < 100 and now - last_progress < 0.25:
                continue
            last_progress = now
            send({"id": request_id, "ok": True, "type": "progress", **progress})

        code = proc.wait(timeout=30)
        if code == 0 and not cancel_event.is_set():
            final = paths.get("final")
            if not final or not os.path.isfile(final):
                final = newest_file(directory, started)
            if not final or not os.path.isfile(final):
                raise RuntimeError("Файл не найден после скачивания")
            send({
                "id": request_id,
                "ok": True,
                "type": "done",
                "path": final,
                "filename": os.path.basename(final),
            })
            return
        if cancel_event.is_set():
            send({"id": request_id, "ok": False, "type": "cancelled", "error": "Скачивание отменено"})
            return
        detail = "\n".join(stderr_lines[-8:]) or f"yt-dlp завершился с кодом {code}"
        raise RuntimeError(clean_ytdlp_error(detail))
    except Exception as exc:
        if cancel_event.is_set():
            send({"id": request_id, "ok": False, "type": "cancelled", "error": "Скачивание отменено"})
        else:
            send({"id": request_id, "ok": False, "type": "error", "error": str(exc)})
    finally:
        if proc is not None and proc.poll() is None:
            kill_process_group(proc)
        with proc_lock:
            if current_proc is proc:
                current_proc = None
            download_active = False


def safe_ping(message: dict) -> None:
    try:
        send({"id": message.get("id"), "ok": True, "type": "config", **tool_status()})
    except Exception as exc:
        send({"id": message.get("id"), "ok": False, "type": "error", "error": str(exc)})


def safe_info(message: dict) -> None:
    try:
        url = canonical_url(message.get("url") or "")
        send({"id": message.get("id"), "ok": True, "type": "formats", **fetch_info(url)})
    except Exception as exc:
        send({"id": message.get("id"), "ok": False, "type": "error", "error": str(exc)})


def begin_download(message: dict) -> None:
    global download_active
    with proc_lock:
        if download_active:
            send({
                "id": message.get("id"),
                "ok": False,
                "type": "error",
                "error": "Уже идёт скачивание",
            })
            return
        download_active = True
    cancel_event.clear()
    thread = threading.Thread(target=run_download, args=(message,))
    workers.append(thread)
    thread.start()


def dispatch(message: dict) -> None:
    if not isinstance(message, dict):
        send({"ok": False, "type": "error", "error": "Некорректное сообщение"})
        return
    action = message.get("action")
    if action == "ping":
        thread = threading.Thread(target=safe_ping, args=(message,))
        workers.append(thread)
        thread.start()
    elif action == "info":
        thread = threading.Thread(target=safe_info, args=(message,))
        workers.append(thread)
        thread.start()
    elif action == "download":
        begin_download(message)
    elif action == "cancel":
        if download_active:
            cancel_download()
        else:
            send({
                "id": message.get("id"),
                "ok": True,
                "type": "cancelled",
                "error": "Скачивание отменено",
            })
    else:
        send({"id": message.get("id"), "ok": False, "type": "error", "error": "Неизвестная команда"})


def main() -> None:
    while True:
        try:
            message = read_message()
        except json.JSONDecodeError:
            send({"ok": False, "type": "error", "error": "Некорректное сообщение"})
            continue
        except ValueError as exc:
            send({"ok": False, "type": "error", "error": str(exc)})
            continue
        if message is None:
            break
        dispatch(message)
    cancel_download()
    for thread in list(workers):
        thread.join()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
