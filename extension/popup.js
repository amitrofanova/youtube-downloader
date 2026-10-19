const status = document.querySelector("#status");
const tools = document.querySelector("#tools");
const hint = document.querySelector("#hint");
const input = document.querySelector("#dir");
const save = document.querySelector("#save");

function toolText(response) {
  const ytdlp = response.ytdlp
    ? `yt-dlp ${response.ytdlpVersion || ""}`.trim()
    : "yt-dlp не найден";
  const ffmpeg = response.ffmpeg
    ? `ffmpeg ${response.ffmpegVersion || ""}`.trim()
    : "ffmpeg не найден";
  return `${ytdlp} · ${ffmpeg}`;
}

async function refresh() {
  const stored = await chrome.storage.local.get({ downloadDir: "" });
  input.value = stored.downloadDir || "";
  const id = crypto.randomUUID();
  chrome.runtime.sendMessage({ action: "ping", id }, (response) => {
    const runtimeError = chrome.runtime.lastError;
    if (runtimeError) {
      status.textContent = runtimeError.message;
      return;
    }
    if (!response || response.ok === false) {
      status.textContent = (response && response.error) || "Хост не ответил";
      return;
    }
    status.textContent = response.ready
      ? "Готово к скачиванию"
      : "Хост подключён, не хватает программ";
    tools.textContent = toolText(response);
    if (!stored.downloadDir && response.downloadDir) {
      input.placeholder = response.downloadDir;
    }
    if (response.hint) {
      hint.textContent = response.hint;
    }
  });
}

save.addEventListener("click", async () => {
  const value = input.value.trim();
  if (value && !value.startsWith("/") && !value.startsWith("~/")) {
    hint.textContent = "Укажите полный путь, например /home/user/Downloads/YouTube";
    return;
  }
  await chrome.storage.local.set({ downloadDir: value });
  hint.textContent = value
    ? "Папка сохранена. Она будет проверена при следующем скачивании."
    : "Будет использована папка по умолчанию ~/Downloads/YouTube.";
});

refresh();
