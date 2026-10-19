#!/usr/bin/env bash
# Регистрирует native messaging host для Chrome, Chromium и Edge.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_PATH="$ROOT/native-host/ytdlp_host.py"
MANIFEST_JSON="$ROOT/extension/manifest.json"
NAME="com.alena.ytdlp"

chmod 755 "$HOST_PATH"

EXT_ID="$(python3 - "$MANIFEST_JSON" <<'PY'
import base64, hashlib, json, sys
manifest = json.loads(open(sys.argv[1], encoding="utf-8").read())
der = base64.b64decode(manifest["key"])
digest = hashlib.sha256(der).hexdigest()[:32]
print("".join(chr(ord("a") + int(c, 16)) for c in digest))
PY
)"

HOST_MANIFEST="$(python3 - "$NAME" "$HOST_PATH" "$EXT_ID" <<'PY'
import json, sys
name, path, ext_id = sys.argv[1:]
print(json.dumps({
    "name": name,
    "description": "yt-dlp host for YouTube Downloader",
    "path": path,
    "type": "stdio",
    "allowed_origins": [f"chrome-extension://{ext_id}/"],
}, indent=2, ensure_ascii=False))
PY
)"

install_to() {
  local config_dir="$1"
  local required="$2"
  if [[ "$required" != "yes" && ! -d "$config_dir" ]]; then
    return 0
  fi
  local dest="$config_dir/NativeMessagingHosts"
  mkdir -p "$dest"
  printf '%s\n' "$HOST_MANIFEST" > "$dest/$NAME.json"
  chmod 644 "$dest/$NAME.json"
  echo "Установлено: $dest/$NAME.json"
}

install_to "${HOME}/.config/google-chrome" yes
install_to "${HOME}/.config/chromium" no
install_to "${HOME}/.config/microsoft-edge" no
# Сборка Chromium GOST ищет хосты в своём каталоге, а не в ~/.config/chromium.
if [[ -x /usr/bin/chromium-gost || -d "${HOME}/.config/chromium-gost" ]]; then
  install_to "${HOME}/.config/chromium-gost" yes
fi

echo "ID расширения: $EXT_ID"

if ! command -v yt-dlp >/dev/null 2>&1; then
  echo "Не найден yt-dlp. Установите: pip install -U yt-dlp"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Не найден ffmpeg. Установите: sudo apt install ffmpeg"
fi
