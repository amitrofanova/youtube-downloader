const HOST_ID = "ytdlp-dl-host";
const PANEL_ID = "ytdlp-dl-panel";
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;

const state = {
  videoId: "",
  url: "",
  requestId: "",
  title: "",
  channel: "",
  duration: null,
  formats: [],
  mode: "idle",
  error: "",
  path: "",
  filename: "",
  percent: null,
  speed: "",
  eta: "",
  status: "",
  insertAttempts: 0,
};

const observedRoots = new WeakSet();
let buttonHost = null;
let panelHost = null;
let scheduled = false;
let dockRequested = true;

const SHADOW_CSS = `
  :host {
    --bg: #ffffff;
    --fg: #0f0f0f;
    --muted: #606060;
    --line: #e5e5e5;
    --chip: rgba(0, 0, 0, 0.05);
    --chip-hover: rgba(0, 0, 0, 0.1);
    --accent: #c40000;
    --accent-fg: #ffffff;
    --ok: #0d7a32;
    --danger: #c40000;
    color-scheme: light;
  }
  :host([data-theme="dark"]) {
    --bg: #212121;
    --fg: #f1f1f1;
    --muted: #aaaaaa;
    --line: #3f3f3f;
    --chip: rgba(255, 255, 255, 0.1);
    --chip-hover: rgba(255, 255, 255, 0.16);
    --accent: #ff4e45;
    --accent-fg: #ffffff;
    --ok: #7dcea0;
    --danger: #ff8a80;
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  button, p, h2 { font: 13px/1.35 system-ui, sans-serif; }
  .launch {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 36px;
    padding: 0 14px 0 12px;
    border: 0;
    border-radius: 18px;
    background: var(--chip);
    color: var(--fg);
    cursor: pointer;
    font-weight: 550;
  }
  .launch:hover, .launch[aria-expanded="true"] { background: var(--chip-hover); }
  .launch:focus-visible, .choice:focus-visible, .ghost:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .launch svg { display: block; }
  .panel {
    width: 300px;
    padding: 12px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--bg);
    color: var(--fg);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
  }
  .panel[hidden] { display: none; }
  h2 {
    margin: 0;
    font-size: 14px;
    font-weight: 650;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .meta, .status, .path { margin: 6px 0 0; color: var(--muted); word-break: break-word; }
  .error { margin: 6px 0 0; color: var(--danger); white-space: pre-wrap; word-break: break-word; }
  .ok { margin: 6px 0 0; color: var(--ok); font-weight: 650; }
  .formats { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
  .choice, .ghost {
    width: 100%;
    border: 0;
    border-radius: 8px;
    padding: 8px 10px;
    background: var(--chip);
    color: var(--fg);
    cursor: pointer;
  }
  .choice { text-align: left; }
  .choice:hover, .ghost:hover { background: var(--chip-hover); }
  .row { display: flex; align-items: center; gap: 8px; margin-top: 10px; color: var(--muted); }
  .spinner {
    width: 16px;
    height: 16px;
    flex: 0 0 auto;
    border: 2px solid var(--line);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  .bar {
    height: 6px;
    margin-top: 10px;
    border-radius: 99px;
    background: var(--chip);
    overflow: hidden;
  }
  .fill { height: 100%; width: 0; background: var(--accent); }
  .ghost { margin-top: 10px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) {
    .spinner { animation: none; }
  }
`;

function currentVideoUrl() {
  const url = new URL(location.href);
  if (url.pathname === "/watch") {
    const id = url.searchParams.get("v") || "";
    return VIDEO_ID.test(id) ? `https://www.youtube.com/watch?v=${id}` : "";
  }
  const shorts = url.pathname.match(/^\/shorts\/([A-Za-z0-9_-]{11})/);
  return shorts ? `https://www.youtube.com/watch?v=${shorts[1]}` : "";
}

function isDark() {
  return document.documentElement.hasAttribute("dark")
    || window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function applyTheme() {
  const theme = isDark() ? "dark" : "light";
  if (buttonHost) {
    buttonHost.dataset.theme = theme;
  }
  if (panelHost) {
    panelHost.dataset.theme = theme;
  }
}

function deepQuery(root, selector) {
  if (!root) {
    return null;
  }
  if (root.nodeType === 1 && root.matches?.(selector)) {
    return root;
  }
  const light = root.querySelector?.(selector);
  if (light) {
    return light;
  }
  const elements = root.querySelectorAll ? root.querySelectorAll("*") : [];
  for (const element of elements) {
    if (element.shadowRoot) {
      const found = deepQuery(element.shadowRoot, selector);
      if (found) {
        return found;
      }
    }
  }
  return null;
}

function visible(element) {
  const rect = element.getBoundingClientRect();
  return rect.width > 20 && rect.height > 16;
}

function findAnchor() {
  const scopes = [
    document.querySelector("ytd-watch-metadata"),
    document.querySelector("ytd-watch-flexy"),
    document.querySelector("ytd-shorts"),
    document.querySelector("ytd-page-manager"),
  ].filter(Boolean);
  const selectors = location.pathname.startsWith("/shorts/")
    ? ["#actions", "ytd-reel-player-overlay-renderer"]
    : ["#top-level-buttons-computed", "#actions-inner", "#actions"];
  for (const scope of scopes) {
    for (const selector of selectors) {
      const found = deepQuery(scope, selector);
      if (found && visible(found)) {
        return found;
      }
    }
  }
  return null;
}

function watchRoot(root) {
  if (!root || observedRoots.has(root)) {
    return;
  }
  observedRoots.add(root);
  new MutationObserver(onDomChange).observe(root, { childList: true, subtree: true });
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) {
    return "";
  }
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function attachShadow(node) {
  const shadow = node.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = SHADOW_CSS;
  shadow.appendChild(style);
  return shadow;
}

function eventHitsUs(event) {
  const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
  return path.includes(buttonHost) || path.includes(panelHost);
}

function ensureButton() {
  if (!state.url) {
    buttonHost?.remove();
    buttonHost = null;
    return;
  }
  if (!buttonHost || !buttonHost.isConnected) {
    buttonHost = document.createElement("div");
    buttonHost.id = HOST_ID;
    const shadow = attachShadow(buttonHost);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "launch";
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("width", "18");
    icon.setAttribute("height", "18");
    icon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("fill", "currentColor");
    path.setAttribute("d", "M12 3a1 1 0 0 1 1 1v8.2l2.3-2.3a1 1 0 1 1 1.4 1.4l-4 4a1 1 0 0 1-1.4 0l-4-4a1 1 0 1 1 1.4-1.4L11 12.2V4a1 1 0 0 1 1-1zm-7 14a1 1 0 0 1 1 1v1h12v-1a1 1 0 1 1 2 0v2a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-2a1 1 0 0 1 1-1z");
    icon.appendChild(path);
    const label = document.createElement("span");
    button.append(icon, label);
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      togglePanel();
    });
    shadow.append(button);
  }

  const docked = buttonHost.isConnected && buttonHost.parentNode !== document.documentElement;
  if (!docked && dockRequested && state.insertAttempts < 6) {
    dockRequested = false;
    const anchor = findAnchor();
    if (anchor) {
      state.insertAttempts += 1;
      anchor.appendChild(buttonHost);
      buttonHost.classList.remove("floating");
      watchRoot(anchor.getRootNode());
    } else {
      document.documentElement.appendChild(buttonHost);
      buttonHost.classList.add("floating");
    }
  } else if (!buttonHost.isConnected) {
    document.documentElement.appendChild(buttonHost);
    buttonHost.classList.add("floating");
  }

  const button = buttonHost.shadowRoot.querySelector("button");
  const label = buttonHost.shadowRoot.querySelector("span");
  button.setAttribute("aria-expanded", state.mode === "idle" ? "false" : "true");
  label.textContent = state.mode === "downloading" && state.percent != null
    ? `${Math.floor(state.percent)}%`
    : "Скачать";
}

function progressText() {
  const bits = [state.status || "Скачиваю…"];
  if (state.percent != null) {
    bits.push(`${state.percent.toFixed(1)}%`);
  }
  if (state.speed) {
    bits.push(state.speed);
  }
  if (state.eta && state.eta !== "Unknown") {
    bits.push(`осталось ${state.eta}`);
  }
  return bits.join(" · ");
}

function fillPanel(card) {
  card.replaceChildren();
  card.dataset.mode = state.mode;
  const title = document.createElement("h2");
  title.textContent = state.title || "Скачать ролик";
  card.appendChild(title);
  const meta = [state.channel, formatDuration(state.duration)].filter(Boolean).join(" · ");
  if (meta) {
    const metaNode = document.createElement("p");
    metaNode.className = "meta";
    metaNode.textContent = meta;
    card.appendChild(metaNode);
  }

  if (state.mode === "loading") {
    const row = document.createElement("div");
    row.className = "row";
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    const text = document.createElement("span");
    text.textContent = "Получаю форматы…";
    row.append(spinner, text);
    card.appendChild(row);
    return;
  }

  if (state.mode === "list") {
    const list = document.createElement("div");
    list.className = "formats";
    for (const option of state.formats) {
      const choice = document.createElement("button");
      choice.type = "button";
      choice.className = "choice";
      choice.textContent = option.label;
      choice.addEventListener("click", () => startDownload(option));
      list.appendChild(choice);
    }
    card.appendChild(list);
    return;
  }

  if (state.mode === "downloading") {
    const status = document.createElement("p");
    status.className = "status";
    status.textContent = progressText();
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("div");
    fill.className = "fill";
    fill.style.width = `${Math.max(0, Math.min(100, state.percent || 0))}%`;
    bar.appendChild(fill);
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "ghost";
    cancel.textContent = "Отмена";
    cancel.addEventListener("click", cancelDownload);
    card.append(status, bar, cancel);
    return;
  }

  if (state.mode === "done") {
    const ok = document.createElement("p");
    ok.className = "ok";
    ok.textContent = "Сохранено";
    const path = document.createElement("p");
    path.className = "path";
    path.textContent = state.path || state.filename;
    card.append(ok, path);
    return;
  }

  const error = document.createElement("p");
  error.className = "error";
  error.textContent = state.error || "Не удалось скачать ролик";
  card.appendChild(error);
}

function updateDownloading(card) {
  const status = card.querySelector(".status");
  const fill = card.querySelector(".fill");
  if (!status || !fill) {
    fillPanel(card);
    return;
  }
  status.textContent = progressText();
  fill.style.width = `${Math.max(0, Math.min(100, state.percent || 0))}%`;
}

function placePanel() {
  if (!panelHost) {
    return;
  }
  const card = panelHost.shadowRoot.querySelector(".panel");
  const width = 300;
  const height = card.offsetHeight || 220;
  let left = 16;
  let top = 80;
  if (buttonHost) {
    const rect = buttonHost.getBoundingClientRect();
    left = Math.min(Math.max(8, rect.right - width), window.innerWidth - width - 8);
    top = rect.bottom + 8;
    if (top + height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - height - 8);
    }
  }
  panelHost.style.left = `${left}px`;
  panelHost.style.top = `${top}px`;
}

function ensurePanel() {
  if (!state.url) {
    panelHost?.remove();
    panelHost = null;
    return;
  }
  if (!panelHost || !panelHost.isConnected) {
    panelHost = document.createElement("div");
    panelHost.id = PANEL_ID;
    const shadow = attachShadow(panelHost);
    const card = document.createElement("div");
    card.className = "panel";
    card.hidden = true;
    card.addEventListener("click", (event) => event.stopPropagation());
    shadow.appendChild(card);
    document.documentElement.appendChild(panelHost);
  }
  const card = panelHost.shadowRoot.querySelector(".panel");
  card.hidden = state.mode === "idle";
  if (state.mode === "idle") {
    return;
  }
  if (state.mode === "downloading" && card.dataset.mode === "downloading") {
    updateDownloading(card);
  } else {
    fillPanel(card);
  }
  placePanel();
}

function render() {
  applyTheme();
  ensureButton();
  ensurePanel();
}

function resetForVideo(url) {
  const id = new URL(url).searchParams.get("v");
  if (state.videoId === id && state.url === url) {
    return;
  }
  state.videoId = id;
  state.url = url;
  state.requestId = "";
  state.title = "";
  state.channel = "";
  state.duration = null;
  state.formats = [];
  state.mode = "idle";
  state.error = "";
  state.path = "";
  state.filename = "";
  state.percent = null;
  state.speed = "";
  state.eta = "";
  state.status = "";
  state.insertAttempts = 0;
  dockRequested = true;
}

function showError(message) {
  state.mode = "error";
  state.error = message;
  render();
}

function togglePanel() {
  if (state.mode === "downloading") {
    render();
    return;
  }
  if (state.mode !== "idle") {
    state.mode = "idle";
    render();
    return;
  }
  state.mode = "loading";
  state.error = "";
  render();
  const id = crypto.randomUUID();
  state.requestId = id;
  chrome.runtime.sendMessage({ action: "info", id, url: state.url }, (response) => {
    if (state.requestId !== id) {
      return;
    }
    const runtimeError = chrome.runtime.lastError;
    if (runtimeError) {
      showError(runtimeError.message.includes("invalidated")
        ? "Обновите страницу YouTube: расширение перезагрузили."
        : runtimeError.message);
      return;
    }
    if (!response || response.ok === false) {
      showError((response && response.error) || "Не удалось получить форматы");
      return;
    }
    state.title = response.title || "";
    state.channel = response.channel || "";
    state.duration = response.duration;
    state.formats = Array.isArray(response.formats) ? response.formats : [];
    if (!state.formats.length) {
      showError("yt-dlp не нашёл форматов для этого ролика");
      return;
    }
    state.mode = "list";
    render();
  });
}

function startDownload(option) {
  const id = crypto.randomUUID();
  state.requestId = id;
  state.mode = "downloading";
  state.percent = 0;
  state.speed = "";
  state.eta = "";
  state.status = "Запускаю yt-dlp…";
  state.error = "";
  render();
  chrome.runtime.sendMessage({
    action: "download",
    id,
    url: state.url,
    format: option.format,
    merge: Boolean(option.merge),
    extractAudio: option.extractAudio || null,
  }, (response) => {
    if (state.requestId !== id) {
      return;
    }
    const runtimeError = chrome.runtime.lastError;
    if (runtimeError || !response || response.ok === false) {
      showError(runtimeError?.message || response?.error || "Не удалось начать скачивание");
    }
  });
}

function cancelDownload() {
  if (!state.requestId) {
    return;
  }
  chrome.runtime.sendMessage({ action: "cancel", id: state.requestId });
  state.status = "Останавливаю…";
  render();
}

function onNative(message) {
  if (!message || message.id !== state.requestId) {
    return;
  }
  if (message.type === "progress") {
    state.mode = "downloading";
    if (typeof message.percent === "number") {
      state.percent = message.percent;
    }
    state.speed = message.speed || "";
    state.eta = message.eta || "";
    state.status = message.status || "Скачиваю…";
  } else if (message.type === "done") {
    state.mode = "done";
    state.path = message.path || "";
    state.filename = message.filename || "";
    state.percent = 100;
  } else if (message.type === "cancelled") {
    state.mode = "error";
    state.error = message.error || "Скачивание отменено";
  } else if (message.type === "error") {
    state.mode = "error";
    state.error = message.error || "Не удалось скачать ролик";
  } else {
    return;
  }
  render();
}

function syncPage() {
  const url = currentVideoUrl();
  if (!url) {
    state.url = "";
    state.videoId = "";
    state.mode = "idle";
    render();
    return;
  }
  resetForVideo(url);
  render();
}

function onDomChange() {
  const url = currentVideoUrl();
  const buttonMissing = !buttonHost || !buttonHost.isConnected;
  if (!url) {
    if (buttonHost || panelHost) {
      syncPage();
    }
    return;
  }
  if (buttonMissing) {
    dockRequested = true;
    schedule();
  }
}

function schedule() {
  if (scheduled) {
    return;
  }
  scheduled = true;
  setTimeout(() => {
    scheduled = false;
    syncPage();
  }, 300);
}

chrome.runtime.onMessage.addListener((message) => {
  onNative(message);
});

function retryDock() {
  [400, 1200, 2500, 5000].forEach((delay) => {
    setTimeout(() => {
      if (!state.url || (buttonHost?.isConnected && !buttonHost.classList.contains("floating"))) {
        return;
      }
      dockRequested = true;
      render();
    }, delay);
  });
}

document.addEventListener("yt-navigate-finish", () => {
  dockRequested = true;
  syncPage();
  retryDock();
}, true);
document.addEventListener("yt-page-data-updated", () => {
  const url = currentVideoUrl();
  if (url !== state.url || !buttonHost?.isConnected) {
    dockRequested = true;
    schedule();
  }
}, true);
document.addEventListener("click", (event) => {
  if (eventHitsUs(event)) {
    return;
  }
  if (state.mode !== "idle" && state.mode !== "downloading") {
    state.mode = "idle";
    render();
  }
}, true);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.mode !== "idle" && state.mode !== "downloading") {
    state.mode = "idle";
    render();
  }
});
window.addEventListener("resize", () => {
  if (state.mode !== "idle") {
    placePanel();
  }
});
new MutationObserver(applyTheme).observe(document.documentElement, {
  attributes: true,
  attributeFilter: ["dark", "style"],
});
if (document.body) {
  watchRoot(document.body);
}
syncPage();
retryDock();
