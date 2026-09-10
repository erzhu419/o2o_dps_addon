#!/usr/bin/env node

import { spawn } from "node:child_process";
import { access, mkdir, readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";

const OFFICIAL_HOST = "capy.chronicleclassic.com";
const DEFAULT_CHROME = String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`;
const STREAM_COUNT = 17;
const CDP_COMMAND_TIMEOUT_MS = 30_000;
const UI_READY_TIMEOUT_MS = 180_000;

function emit(event, fields = {}) {
  process.stdout.write(`${JSON.stringify({
    timestamp: new Date().toISOString(),
    event,
    ...fields,
  })}\n`);
}

function usage() {
  return `Usage:
  node scripts/chronicle_ui_export.mjs --url <official-instance-url> \\
    --profile-dir <chrome-profile> --download-dir <worker-downloads> [options]

  node scripts/chronicle_ui_export.mjs --inspect-cdp-port <port>

Launch options:
  --chrome <path>             Chrome executable (default: ${DEFAULT_CHROME})
  --cdp-port <port>           Fixed CDP port (default: Chrome chooses a free port)
  --timeout-minutes <number>  Export timeout; 0 means no timeout (default: 0)
  --progress-seconds <number> JSON progress interval (default: 15)

Inspection mode attaches read-only to an existing Chrome CDP port. It does not
click, navigate, change download settings, or close that Chrome instance.
`;
}

function parseNumber(name, value, { integer = false, allowZero = false } = {}) {
  const number = Number(value);
  const valid = Number.isFinite(number)
    && (!integer || Number.isInteger(number))
    && (allowZero ? number >= 0 : number > 0);
  if (!valid) {
    throw new Error(`${name} must be ${allowZero ? "zero or " : ""}a positive${integer ? " integer" : " number"}`);
  }
  return number;
}

function parseArgs(argv) {
  const options = {
    chrome: DEFAULT_CHROME,
    cdpPort: 0,
    timeoutMinutes: 0,
    progressSeconds: 15,
    inspectCdpPort: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--help" || argument === "-h") {
      options.help = true;
      continue;
    }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new Error(`${argument} requires a value`);
    }
    index += 1;
    switch (argument) {
      case "--url":
        options.url = value;
        break;
      case "--profile-dir":
        options.profileDir = path.resolve(value);
        break;
      case "--download-dir":
        options.downloadDir = path.resolve(value);
        break;
      case "--chrome":
        options.chrome = path.resolve(value);
        break;
      case "--cdp-port":
        options.cdpPort = parseNumber(argument, value, { integer: true });
        break;
      case "--inspect-cdp-port":
        options.inspectCdpPort = parseNumber(argument, value, { integer: true });
        break;
      case "--timeout-minutes":
        options.timeoutMinutes = parseNumber(argument, value, { allowZero: true });
        break;
      case "--progress-seconds":
        options.progressSeconds = parseNumber(argument, value);
        break;
      default:
        throw new Error(`unknown argument: ${argument}`);
    }
  }

  if (options.help) return options;
  if (options.inspectCdpPort !== null) {
    if (options.url || options.profileDir || options.downloadDir || options.cdpPort !== 0) {
      throw new Error("--inspect-cdp-port is a standalone read-only mode");
    }
    return options;
  }
  if (!options.url || !options.profileDir || !options.downloadDir) {
    throw new Error("--url, --profile-dir, and --download-dir are required");
  }
  const officialUrl = new URL(options.url);
  if (officialUrl.protocol !== "https:" || officialUrl.hostname !== OFFICIAL_HOST) {
    throw new Error(`--url must be an https://${OFFICIAL_HOST}/ URL`);
  }
  if (!officialUrl.pathname.startsWith("/instances/")) {
    throw new Error("--url must point to an official Chronicle instance page");
  }
  return options;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitUntil(operation, {
  timeoutMs,
  intervalMs = 250,
  description,
}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const result = await operation();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await sleep(intervalMs);
  }
  const suffix = lastError instanceof Error ? `: ${lastError.message}` : "";
  throw new Error(`timed out waiting for ${description}${suffix}`);
}

class CdpConnection {
  constructor(webSocketUrl) {
    this.webSocketUrl = webSocketUrl;
    this.socket = null;
    this.sequence = 0;
    this.pending = new Map();
    this.eventHandlers = new Set();
  }

  async connect() {
    if (typeof WebSocket === "undefined") {
      throw new Error("this script requires a Node.js runtime with global WebSocket support");
    }
    await new Promise((resolve, reject) => {
      const socket = new WebSocket(this.webSocketUrl);
      const timer = setTimeout(() => {
        socket.close();
        reject(new Error(`timed out connecting to CDP: ${this.webSocketUrl}`));
      }, CDP_COMMAND_TIMEOUT_MS);
      socket.addEventListener("open", () => {
        clearTimeout(timer);
        this.socket = socket;
        resolve();
      }, { once: true });
      socket.addEventListener("error", () => {
        clearTimeout(timer);
        reject(new Error(`failed to connect to CDP: ${this.webSocketUrl}`));
      }, { once: true });
      socket.addEventListener("message", (message) => this.handleMessage(message));
      socket.addEventListener("close", () => this.handleClose());
    });
  }

  handleMessage(message) {
    const payload = JSON.parse(String(message.data));
    if (payload.id && this.pending.has(payload.id)) {
      const pending = this.pending.get(payload.id);
      this.pending.delete(payload.id);
      clearTimeout(pending.timer);
      if (payload.error) {
        pending.reject(new Error(`CDP ${pending.method} failed: ${JSON.stringify(payload.error)}`));
      } else {
        pending.resolve(payload.result);
      }
      return;
    }
    if (payload.method) {
      for (const handler of this.eventHandlers) handler(payload.method, payload.params ?? {});
    }
  }

  handleClose() {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error(`CDP connection closed during ${pending.method}`));
    }
    this.pending.clear();
  }

  onEvent(handler) {
    this.eventHandlers.add(handler);
    return () => this.eventHandlers.delete(handler);
  }

  send(method, params = {}) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error(`CDP is not connected for ${method}`));
    }
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP command timed out: ${method}`));
      }, CDP_COMMAND_TIMEOUT_MS);
      this.pending.set(id, { method, resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    if (this.socket && this.socket.readyState < WebSocket.CLOSING) this.socket.close();
  }
}

async function fetchTargets(port) {
  const response = await fetch(`http://127.0.0.1:${port}/json/list`);
  if (!response.ok) throw new Error(`CDP target list returned HTTP ${response.status}`);
  return response.json();
}

async function waitForPageTarget(port, { officialOnly, timeoutMs = UI_READY_TIMEOUT_MS }) {
  return waitUntil(async () => {
    const targets = await fetchTargets(port);
    const pages = targets.filter((target) => target.type === "page" && target.webSocketDebuggerUrl);
    if (officialOnly) {
      return pages.find((target) => {
        try {
          return new URL(target.url).hostname === OFFICIAL_HOST;
        } catch {
          return false;
        }
      });
    }
    return pages[0];
  }, {
    timeoutMs,
    intervalMs: 250,
    description: `a ${officialOnly ? "Chronicle " : ""}page target on CDP port ${port}`,
  });
}

async function evaluate(cdp, expression) {
  const response = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (response.exceptionDetails) {
    const detail = response.exceptionDetails.exception?.description
      ?? response.exceptionDetails.text
      ?? JSON.stringify(response.exceptionDetails);
    throw new Error(`page evaluation failed: ${detail}`);
  }
  return response.result?.value;
}

const PAGE_SNAPSHOT_EXPRESSION = String.raw`(() => {
  const buttons = Array.from(document.querySelectorAll('button'));
  const enables = buttons.filter((button) =>
    (button.getAttribute('aria-label') || '').startsWith('Enable ')
    && (button.getAttribute('aria-label') || '').includes(' stream (')
  );
  const disables = buttons.filter((button) =>
    (button.getAttribute('aria-label') || '').startsWith('Disable ')
    && (button.getAttribute('aria-label') || '').includes(' stream (')
  );
  const placeholders = [
    'Filter by source...',
    'Filter by ability...',
    'Filter by target...',
  ];
  const filters = placeholders.map((placeholder) => {
    const input = document.querySelector('input[placeholder="' + placeholder + '"]');
    return { placeholder, present: Boolean(input), value: input ? input.value : null };
  });
  const exportButton = buttons.find((button) =>
    button.getAttribute('title') === 'Export all filtered activity pages to CSV'
  );
  const bodyText = document.body ? document.body.innerText : '';
  return {
    href: location.href,
    title: document.title,
    readyState: document.readyState,
    hasSelectAll: Boolean(buttons.find((button) => button.getAttribute('title') === 'Select all encounters')),
    hasShowEncounters: Boolean(buttons.find((button) => button.getAttribute('title') === 'Show encounters')),
    encounterSelection: bodyText.match(/\d+\s+Encounters Selected/)?.[0] ?? null,
    enableCount: enables.length,
    disableCount: disables.length,
    enableLabels: enables.map((button) => button.getAttribute('aria-label')),
    disableLabels: disables.map((button) => button.getAttribute('aria-label')),
    filters,
    exportButton: exportButton ? {
      text: exportButton.innerText.trim(),
      disabled: exportButton.disabled,
      title: exportButton.getAttribute('title'),
    } : null,
  };
})()`;

async function pageSnapshot(cdp) {
  return evaluate(cdp, PAGE_SNAPSHOT_EXPRESSION);
}

function inspectProblems(snapshot) {
  const problems = [];
  let hostname = null;
  try {
    hostname = new URL(snapshot.href).hostname;
  } catch {
    problems.push("page URL is invalid");
  }
  if (hostname !== OFFICIAL_HOST) problems.push("target is not an official Chronicle page");
  if (!snapshot.hasSelectAll && !snapshot.hasShowEncounters) {
    problems.push("encounter selection controls are missing");
  }
  if (snapshot.enableCount + snapshot.disableCount !== STREAM_COUNT) {
    problems.push(`expected ${STREAM_COUNT} stream buttons, found ${snapshot.enableCount + snapshot.disableCount}`);
  }
  if (snapshot.enableCount !== 0 || snapshot.disableCount !== STREAM_COUNT) {
    problems.push(`streams are not all enabled (${snapshot.disableCount}/${STREAM_COUNT})`);
  }
  for (const filter of snapshot.filters) {
    if (!filter.present) problems.push(`${filter.placeholder} input is missing`);
    else if (filter.value !== "") problems.push(`${filter.placeholder} is not empty`);
  }
  if (!snapshot.exportButton) problems.push("Export CSV button is missing");
  return problems;
}

async function inspectExisting(port) {
  const target = await waitForPageTarget(port, { officialOnly: true });
  const cdp = new CdpConnection(target.webSocketDebuggerUrl);
  await cdp.connect();
  try {
    const snapshot = await pageSnapshot(cdp);
    const problems = inspectProblems(snapshot);
    emit("inspect", {
      mode: "read_only",
      cdpPort: port,
      targetId: target.id,
      ok: problems.length === 0,
      problems,
      snapshot,
    });
    return problems.length === 0 ? 0 : 2;
  } finally {
    cdp.close();
  }
}

async function waitForDevToolsPort(profileDir, launchedAtMs) {
  const activePortFile = path.join(profileDir, "DevToolsActivePort");
  return waitUntil(async () => {
    const info = await stat(activePortFile);
    if (info.mtimeMs < launchedAtMs - 2_000) return null;
    const text = await readFile(activePortFile, "utf8");
    const port = Number(text.split(/\r?\n/, 1)[0]);
    return Number.isInteger(port) && port > 0 ? port : null;
  }, {
    timeoutMs: CDP_COMMAND_TIMEOUT_MS,
    intervalMs: 100,
    description: "Chrome DevToolsActivePort",
  });
}

function launchChrome(options) {
  const launchedAtMs = Date.now();
  const args = [
    "--headless=new",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--remote-allow-origins=*",
    `--remote-debugging-port=${options.cdpPort}`,
    `--user-data-dir=${options.profileDir}`,
    "about:blank",
  ];
  const child = spawn(options.chrome, args, {
    stdio: "ignore",
    windowsHide: true,
  });
  emit("chrome_launched", {
    pid: child.pid,
    profileDir: options.profileDir,
    downloadDir: options.downloadDir,
    requestedCdpPort: options.cdpPort,
  });
  return { child, launchedAtMs };
}

function waitForChildExit(child) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve({ code: child.exitCode, signal: child.signalCode });
  }
  return new Promise((resolve) => {
    child.once("exit", (code, signal) => resolve({ code, signal }));
  });
}

async function forceCloseSpawnedChrome(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32") {
    await new Promise((resolve) => {
      const killer = spawn("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
      killer.once("exit", resolve);
      killer.once("error", resolve);
    });
  } else {
    child.kill("SIGTERM");
  }
}

async function closeOwnedChrome(cdp, child) {
  if (cdp) {
    try {
      await cdp.send("Browser.close");
    } catch {
      // Browser.close commonly closes the WebSocket before returning its response.
    }
    cdp.close();
  }
  if (!child) return;
  const exited = await Promise.race([
    waitForChildExit(child).then(() => true),
    sleep(10_000).then(() => false),
  ]);
  if (!exited) {
    await forceCloseSpawnedChrome(child);
    await Promise.race([waitForChildExit(child), sleep(5_000)]);
  }
  emit("chrome_closed", { pid: child.pid });
}

async function selectAllEncounters(cdp) {
  let result = await evaluate(cdp, String.raw`(() => {
    const button = Array.from(document.querySelectorAll('button')).find((candidate) =>
      candidate.getAttribute('title') === 'Select all encounters'
    );
    if (!button) {
      const show = Array.from(document.querySelectorAll('button')).find((candidate) =>
        candidate.getAttribute('title') === 'Show encounters'
      );
      if (!show) return { clicked: false, openedSidebar: false };
      show.click();
      return { clicked: false, openedSidebar: true };
    }
    button.click();
    return { clicked: true, openedSidebar: false, text: button.innerText.trim() };
  })()`);
  if (result?.openedSidebar) {
    await waitUntil(async () => (await pageSnapshot(cdp)).hasSelectAll, {
      timeoutMs: UI_READY_TIMEOUT_MS,
      description: "Select all encounters after opening the encounter sidebar",
    });
    result = await evaluate(cdp, String.raw`(() => {
      const button = Array.from(document.querySelectorAll('button')).find((candidate) =>
        candidate.getAttribute('title') === 'Select all encounters'
      );
      if (!button) return { clicked: false };
      button.click();
      return { clicked: true, text: button.innerText.trim() };
    })()`);
  }
  if (!result?.clicked) throw new Error("Select all encounters control is missing");
  await sleep(300);
  await waitUntil(async () => (await pageSnapshot(cdp)).encounterSelection, {
    timeoutMs: UI_READY_TIMEOUT_MS,
    description: "the All encounters selection",
  });
  const snapshot = await pageSnapshot(cdp);
  emit("encounters_selected", { selection: snapshot.encounterSelection });
}

async function enableAllStreams(cdp) {
  for (let clicks = 0; clicks <= STREAM_COUNT; clicks += 1) {
    const snapshot = await pageSnapshot(cdp);
    const total = snapshot.enableCount + snapshot.disableCount;
    if (total !== STREAM_COUNT) {
      throw new Error(`expected ${STREAM_COUNT} stream buttons, found ${total}`);
    }
    if (snapshot.enableCount === 0) {
      if (snapshot.disableCount !== STREAM_COUNT) {
        throw new Error(`expected ${STREAM_COUNT} enabled streams, found ${snapshot.disableCount}`);
      }
      emit("streams_enabled", { count: snapshot.disableCount });
      return;
    }
    const previousDisableCount = snapshot.disableCount;
    const clicked = await evaluate(cdp, String.raw`(() => {
      const button = Array.from(document.querySelectorAll('button')).find((candidate) => {
        const label = candidate.getAttribute('aria-label') || '';
        return label.startsWith('Enable ') && label.includes(' stream (');
      });
      if (!button) return null;
      const label = button.getAttribute('aria-label');
      button.click();
      return label;
    })()`);
    if (!clicked) throw new Error("an Enable stream button disappeared before it could be clicked");
    await waitUntil(async () => {
      const next = await pageSnapshot(cdp);
      return next.disableCount > previousDisableCount ? next : null;
    }, {
      timeoutMs: UI_READY_TIMEOUT_MS,
      description: `stream state update after ${clicked}`,
    });
    emit("stream_enabled", { label: clicked, enabledCount: previousDisableCount + 1 });
  }
  throw new Error(`more than ${STREAM_COUNT} stream clicks were required`);
}

function assertEmptyFilters(snapshot) {
  for (const filter of snapshot.filters) {
    if (!filter.present) throw new Error(`${filter.placeholder} input is missing`);
    if (filter.value !== "") throw new Error(`${filter.placeholder} must be empty`);
  }
  emit("filters_verified", {
    filters: snapshot.filters.map((filter) => ({
      placeholder: filter.placeholder,
      value: filter.value,
    })),
  });
}

async function clickExportOnce(cdp) {
  return evaluate(cdp, String.raw`(() => {
    const button = Array.from(document.querySelectorAll('button')).find((candidate) =>
      candidate.getAttribute('title') === 'Export all filtered activity pages to CSV'
    );
    if (!button) return { clicked: false, reason: 'missing' };
    const text = button.innerText.trim();
    if (button.disabled) return { clicked: false, reason: 'disabled', text };
    if (text !== 'Export CSV') return { clicked: false, reason: 'unexpected_text', text };
    button.click();
    return { clicked: true, text };
  })()`);
}

async function directoryVersions(directory) {
  const versions = new Map();
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (!entry.isFile()) continue;
    const filePath = path.join(directory, entry.name);
    const info = await stat(filePath);
    versions.set(entry.name, { size: info.size, mtimeMs: info.mtimeMs });
  }
  return versions;
}

async function waitForCompletedCsv(downloadDir, baseline, clickedAtMs) {
  let previous = null;
  return waitUntil(async () => {
    const versions = await directoryVersions(downloadDir);
    const activePartial = [...versions.entries()].some(([name, version]) =>
      name.endsWith(".crdownload") && version.mtimeMs >= clickedAtMs - 2_000
    );
    if (activePartial) {
      previous = null;
      return null;
    }
    const candidates = [...versions.entries()]
      .filter(([name, version]) => {
        if (!name.toLowerCase().endsWith(".csv") || version.size <= 0) return false;
        const old = baseline.get(name);
        return !old || old.size !== version.size || old.mtimeMs !== version.mtimeMs;
      })
      .sort((left, right) => right[1].mtimeMs - left[1].mtimeMs);
    if (candidates.length === 0) {
      previous = null;
      return null;
    }
    const [name, version] = candidates[0];
    if (previous && previous.name === name && previous.size === version.size && previous.mtimeMs === version.mtimeMs) {
      return { path: path.join(downloadDir, name), ...version };
    }
    previous = { name, ...version };
    return null;
  }, {
    timeoutMs: 30_000,
    intervalMs: 500,
    description: "the completed CSV file to become visible in the worker download directory",
  });
}

function parseExportProgress(text) {
  const match = /^Exporting\s+(\d+)\s*\/\s*(\d+)$/.exec(text ?? "");
  return match ? { page: Number(match[1]), totalPages: Number(match[2]) } : null;
}

async function runExport(options) {
  await access(options.chrome);
  await mkdir(options.profileDir, { recursive: true });
  await mkdir(options.downloadDir, { recursive: true });

  let child = null;
  let cdp = null;
  let closing = false;
  const close = async () => {
    if (closing) return;
    closing = true;
    await closeOwnedChrome(cdp, child);
  };
  const signalHandler = (signal) => {
    emit("signal", { signal });
    void close().finally(() => process.exit(signal === "SIGINT" ? 130 : 143));
  };
  process.once("SIGINT", signalHandler);
  process.once("SIGTERM", signalHandler);

  try {
    const launched = launchChrome(options);
    child = launched.child;
    const childExit = waitForChildExit(child);
    child.once("error", (error) => emit("chrome_process_error", { message: error.message }));
    const port = options.cdpPort || await waitForDevToolsPort(options.profileDir, launched.launchedAtMs);
    emit("cdp_ready", { port });

    const target = await waitForPageTarget(port, { officialOnly: false });
    cdp = new CdpConnection(target.webSocketDebuggerUrl);
    await cdp.connect();
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Browser.setDownloadBehavior", {
      behavior: "allow",
      downloadPath: options.downloadDir,
      eventsEnabled: true,
    });

    let download = null;
    let resolveDownload;
    let rejectDownload;
    const downloadDone = new Promise((resolve, reject) => {
      resolveDownload = resolve;
      rejectDownload = reject;
    });
    cdp.onEvent((method, params) => {
      if (method === "Browser.downloadWillBegin") {
        download = {
          guid: params.guid,
          suggestedFilename: params.suggestedFilename,
          state: "begun",
          receivedBytes: 0,
          totalBytes: 0,
        };
        emit("download_begin", {
          guid: download.guid,
          suggestedFilename: download.suggestedFilename,
        });
      } else if (method === "Browser.downloadProgress" && (!download || params.guid === download.guid)) {
        download = {
          ...(download ?? { guid: params.guid }),
          state: params.state,
          receivedBytes: params.receivedBytes,
          totalBytes: params.totalBytes,
        };
        if (params.state === "completed") resolveDownload(download);
        else if (params.state === "canceled") rejectDownload(new Error("Chrome canceled the CSV download"));
      }
    });

    await cdp.send("Page.navigate", { url: options.url });
    emit("navigated", { url: options.url });
    const ready = await waitUntil(async () => {
      const snapshot = await pageSnapshot(cdp);
      const streamTotal = snapshot.enableCount + snapshot.disableCount;
      return snapshot.readyState === "complete"
        && (snapshot.hasSelectAll || snapshot.hasShowEncounters)
        && streamTotal === STREAM_COUNT
        && snapshot.exportButton
        ? snapshot
        : null;
    }, {
      timeoutMs: UI_READY_TIMEOUT_MS,
      description: "the official All Activity UI",
    });
    emit("ui_ready", {
      url: ready.href,
      encounterSelection: ready.encounterSelection,
      enabledStreams: ready.disableCount,
      disabledStreams: ready.enableCount,
    });

    await selectAllEncounters(cdp);
    await enableAllStreams(cdp);
    const verified = await pageSnapshot(cdp);
    assertEmptyFilters(verified);
    if (verified.disableCount !== STREAM_COUNT || verified.enableCount !== 0) {
      throw new Error(`stream verification failed (${verified.disableCount}/${STREAM_COUNT} enabled)`);
    }

    const exportReady = await waitUntil(async () => {
      const snapshot = await pageSnapshot(cdp);
      return snapshot.exportButton?.text === "Export CSV" && !snapshot.exportButton.disabled
        ? snapshot
        : null;
    }, {
      timeoutMs: UI_READY_TIMEOUT_MS,
      description: "the enabled Export CSV button",
    });
    const baseline = await directoryVersions(options.downloadDir);
    const clickedAtMs = Date.now();
    const clickResult = await clickExportOnce(cdp);
    if (!clickResult?.clicked) {
      throw new Error(`official Export CSV was not clicked: ${clickResult?.reason ?? "unknown"}${clickResult?.text ? ` (${clickResult.text})` : ""}`);
    }
    emit("export_clicked", {
      url: exportReady.href,
      clickedAt: new Date(clickedAtMs).toISOString(),
    });

    const timeoutAt = options.timeoutMinutes > 0
      ? clickedAtMs + options.timeoutMinutes * 60_000
      : Number.POSITIVE_INFINITY;
    let completedDownload = null;
    while (!completedDownload) {
      const remainingMs = timeoutAt - Date.now();
      if (remainingMs <= 0) {
        throw new Error(`CSV export timed out after ${options.timeoutMinutes} minutes`);
      }
      const waitMs = Math.min(options.progressSeconds * 1_000, remainingMs);
      const outcome = await Promise.race([
        downloadDone.then((value) => ({ type: "download", value })),
        childExit.then((value) => ({ type: "chrome_exit", value })),
        sleep(waitMs).then(() => ({ type: "progress" })),
      ]);
      if (outcome.type === "download") {
        completedDownload = outcome.value;
        break;
      }
      if (outcome.type === "chrome_exit") {
        throw new Error(`Chrome exited before the CSV completed: ${JSON.stringify(outcome.value)}`);
      }
      const snapshot = await pageSnapshot(cdp);
      const progress = parseExportProgress(snapshot.exportButton?.text);
      emit("progress", {
        page: progress?.page ?? null,
        totalPages: progress?.totalPages ?? null,
        buttonText: snapshot.exportButton?.text ?? null,
        download: download ? {
          state: download.state,
          receivedBytes: download.receivedBytes,
          totalBytes: download.totalBytes,
          suggestedFilename: download.suggestedFilename,
        } : null,
      });
    }

    const csv = await waitForCompletedCsv(options.downloadDir, baseline, clickedAtMs);
    emit("completed", {
      guid: completedDownload.guid,
      suggestedFilename: completedDownload.suggestedFilename ?? null,
      receivedBytes: completedDownload.receivedBytes,
      totalBytes: completedDownload.totalBytes,
      csvPath: csv.path,
      csvBytes: csv.size,
    });
    return 0;
  } finally {
    process.removeListener("SIGINT", signalHandler);
    process.removeListener("SIGTERM", signalHandler);
    await close();
  }
}

async function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n\n${usage()}`);
    return 2;
  }
  if (options.help) {
    process.stdout.write(usage());
    return 0;
  }
  if (options.inspectCdpPort !== null) return inspectExisting(options.inspectCdpPort);
  return runExport(options);
}

try {
  process.exitCode = await main();
} catch (error) {
  emit("error", {
    message: error instanceof Error ? error.message : String(error),
    stack: error instanceof Error ? error.stack : null,
  });
  process.exitCode = 1;
}
