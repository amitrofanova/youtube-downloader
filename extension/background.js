const HOST_NAME = "com.alena.ytdlp";

let port = null;
const pending = new Map();
const subscribers = new Map();

function explainDisconnect(reason) {
  const text = reason || "";
  let message = "Нет связи с локальным хостом yt-dlp.";
  if (/not found/i.test(text)) {
    message = "Хост не найден. В каталоге проекта выполните native-host/install.sh и нажмите «Обновить» на chrome://extensions.";
  } else if (/forbidden/i.test(text)) {
    message = "Chrome запретил доступ к хосту. Запустите native-host/install.sh ещё раз: ID расширения должен совпасть с allowed_origins.";
  } else if (/exited|communicating/i.test(text)) {
    message = "Хост yt-dlp закрылся. Проверьте, что python3 установлен, а native-host/ytdlp_host.py можно запустить.";
  }
  return text ? `${message} (${text})` : message;
}

function replyPending(id, message) {
  const respond = pending.get(id);
  if (!respond) {
    return;
  }
  pending.delete(id);
  try {
    respond(message);
  } catch (_error) {
    // Канал сообщения уже закрыт.
  }
}

function failEveryone(error) {
  for (const [id, respond] of pending) {
    try {
      respond({ id, ok: false, type: "error", error });
    } catch (_sendError) {
      // Канал сообщения уже закрыт.
    }
  }
  pending.clear();
  for (const [id, tabId] of subscribers) {
    chrome.tabs.sendMessage(tabId, { id, ok: false, type: "error", error }).catch(() => {});
  }
  subscribers.clear();
}

function connect() {
  if (port) {
    return port;
  }
  port = chrome.runtime.connectNative(HOST_NAME);
  port.onMessage.addListener((message) => {
    if (!message || typeof message !== "object") {
      return;
    }
    if (message.type !== "progress") {
      replyPending(message.id, message);
    }
    const tabId = subscribers.get(message.id);
    if (typeof tabId === "number") {
      chrome.tabs.sendMessage(tabId, message).catch(() => {});
    }
    if (message.type === "done" || message.type === "error" || message.type === "cancelled") {
      subscribers.delete(message.id);
    }
  });
  port.onDisconnect.addListener(() => {
    const reason = chrome.runtime.lastError && chrome.runtime.lastError.message;
    port = null;
    failEveryone(explainDisconnect(reason));
  });
  return port;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.action) {
    return;
  }

  (async () => {
    const payload = { ...message };
    if (payload.action === "download") {
      const stored = await chrome.storage.local.get({ downloadDir: "" });
      payload.downloadDir = stored.downloadDir || "";
    }
    if ((payload.action === "download" || payload.action === "cancel") && sender.tab && sender.tab.id != null) {
      subscribers.set(payload.id, sender.tab.id);
    }
    if (payload.action === "ping" || payload.action === "info") {
      pending.set(payload.id, sendResponse);
    }
    try {
      connect().postMessage(payload);
    } catch (error) {
      pending.delete(payload.id);
      subscribers.delete(payload.id);
      sendResponse({
        id: payload.id,
        ok: false,
        type: "error",
        error: explainDisconnect(error && error.message),
      });
      return;
    }
    if (payload.action !== "ping" && payload.action !== "info") {
      sendResponse({ id: payload.id, ok: true, started: true });
    }
  })().catch((error) => {
    sendResponse({ ok: false, type: "error", error: String(error) });
  });

  return true;
});
