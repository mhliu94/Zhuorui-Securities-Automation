"use strict";

const state = {
  status: null,
  csrfToken: null,
  busyAction: null,
  clockTimer: null,
  pollTimer: null,
};

const elements = Object.fromEntries(
  [
    "account-id", "server-id", "script-card", "script-status", "script-pid",
    "script-started", "script-duration", "script-message", "script-start", "script-stop",
    "emulator-card", "emulator-status", "emulator-avd", "emulator-device",
    "emulator-availability", "emulator-message", "emulator-start", "emulator-stop",
    "health-overall", "health-summary", "health-machine-cpu-value", "health-machine-cpu-detail",
    "health-machine-cpu-level", "health-machine-memory-value", "health-machine-memory-detail",
    "health-machine-memory-level", "health-emulator-memory-value", "health-emulator-memory-detail",
    "health-emulator-memory-level", "health-adb-health-value", "health-adb-health-detail",
    "health-adb-health-level", "health-android-response-value", "health-android-response-detail",
    "health-android-response-level",
    "last-checked", "next-check", "refresh-status", "header-clock", "toast-region", "sign-out",
  ].map((id) => [id, document.getElementById(id)])
);

function localDateTime(isoValue) {
  if (!isoValue) return "—";
  const value = new Date(isoValue);
  if (Number.isNaN(value.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(value);
}

function clockTime(value = new Date()) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(value);
}

function durationText(totalSeconds) {
  if (totalSeconds === null || totalSeconds === undefined || Number.isNaN(Number(totalSeconds))) return "—";
  let remaining = Math.max(0, Math.floor(Number(totalSeconds)));
  const days = Math.floor(remaining / 86400);
  remaining %= 86400;
  const hours = Math.floor(remaining / 3600);
  remaining %= 3600;
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  const time = [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
  return days ? `${days}d ${time}` : time;
}

function countdownText(isoValue) {
  if (!isoValue) return "—";
  const remaining = Math.max(0, Math.ceil((new Date(isoValue).getTime() - Date.now()) / 1000));
  return remaining > 0 ? `in ${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}` : "due now";
}

function statusLabel(status) {
  const labels = {
    running: "Running",
    stopped: "Stopped",
    starting: "Starting",
    booting: "Booting",
    attention: "Attention",
    unavailable: "Unavailable",
  };
  return labels[status] || "Unknown";
}

function paintStatus(target, status) {
  const normalized = status || "unavailable";
  target.className = `status-pill is-${normalized}`;
  target.querySelector("strong").textContent = statusLabel(normalized);
}

function paintHealthLevel(target, level, label) {
  const normalized = ["healthy", "under_load", "restart_recommended"].includes(level) ? level : "under_load";
  target.className = `health-level is-${normalized.replaceAll("_", "-")}`;
  const textTarget = target.querySelector("strong, b");
  if (textTarget) textTarget.textContent = label || "Under load";
}

function renderHealth(health) {
  const metricElements = {
    machine_cpu: "machine-cpu",
    machine_memory: "machine-memory",
    emulator_memory: "emulator-memory",
    adb_health: "adb-health",
    android_response: "android-response",
  };
  paintHealthLevel(elements["health-overall"], health.overall_level, health.overall_label);
  elements["health-summary"].textContent = health.summary || "Health information is unavailable.";
  (health.metrics || []).forEach((metric) => {
    const elementName = metricElements[metric.id];
    if (!elementName) return;
    elements[`health-${elementName}-value`].textContent = metric.value || "Unavailable";
    elements[`health-${elementName}-detail`].textContent = metric.detail || "No detail available.";
    paintHealthLevel(elements[`health-${elementName}-level`], metric.level, metric.level_label);
  });
}

function setButtonState() {
  const scriptRunning = Boolean(state.status?.script?.running);
  const emulatorRunning = Boolean(state.status?.emulator?.running);
  const busy = Boolean(state.busyAction);
  elements["script-start"].disabled = busy || scriptRunning;
  elements["script-stop"].disabled = busy || !scriptRunning;
  elements["emulator-start"].disabled = busy || emulatorRunning;
  elements["emulator-stop"].disabled = busy || !emulatorRunning;
  elements["refresh-status"].disabled = busy;
}

function renderStatus(payload) {
  state.status = payload;
  const script = payload.script || {};
  const emulator = payload.emulator || {};
  const account = payload.account || {};

  elements["account-id"].textContent = account.account_id || "Account not configured";
  elements["server-id"].textContent = account.server_id || "Zhuorui server";

  paintStatus(elements["script-status"], script.state);
  elements["script-card"].dataset.state = script.state || "unavailable";
  elements["script-pid"].textContent = script.running && script.pid ? String(script.pid) : "Not running";
  elements["script-started"].textContent = script.running ? localDateTime(script.started_at) : "—";
  elements["script-duration"].textContent = script.running ? durationText(script.duration_seconds) : "Not running";
  elements["script-message"].textContent = script.message || "Listener status unavailable.";

  paintStatus(elements["emulator-status"], emulator.state);
  elements["emulator-card"].dataset.state = emulator.state || "unavailable";
  elements["emulator-avd"].textContent = emulator.avd || "Not configured";
  elements["emulator-device"].textContent = emulator.device || "Not configured";
  elements["emulator-availability"].textContent = statusLabel(emulator.state);
  elements["emulator-message"].textContent = emulator.message || "Emulator status unavailable.";

  renderHealth(payload.health || {
    overall_level: "under_load",
    overall_label: "Under load",
    summary: "Health information is unavailable.",
    metrics: [],
  });

  elements["last-checked"].textContent = localDateTime(payload.checked_at);
  elements["next-check"].textContent = countdownText(payload.next_check_at);
  setButtonState();
}

function tick() {
  const now = new Date();
  elements["header-clock"].textContent = clockTime(now);
  if (!state.status) return;
  const script = state.status.script;
  if (script?.running && script.started_at) {
    const seconds = Math.max(0, Math.floor((now.getTime() - new Date(script.started_at).getTime()) / 1000));
    elements["script-duration"].textContent = durationText(seconds);
  }
  elements["next-check"].textContent = countdownText(state.status.next_check_at);
}

function toast(message, isError = false) {
  const node = document.createElement("div");
  node.className = `toast${isError ? " is-error" : ""}`;
  node.textContent = message;
  elements["toast-region"].appendChild(node);
  window.setTimeout(() => node.remove(), 6000);
}

async function fetchStatus(force = false) {
  elements["refresh-status"].disabled = true;
  try {
    const response = await fetch(`/api/status${force ? "?refresh=1" : ""}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    if (!response.ok) throw new Error(`Status request failed (${response.status}).`);
    renderStatus(await response.json());
  } catch (error) {
    toast(error.message || "Could not reach the local monitor.", true);
  } finally {
    setButtonState();
  }
}

async function performAction(button) {
  const action = button.dataset.action;
  const confirmation = button.dataset.confirm;
  if (confirmation && !window.confirm(confirmation)) return;

  state.busyAction = action;
  const originalLabel = button.innerHTML;
  button.textContent = "Working…";
  setButtonState();
  try {
    const response = await fetch(`/api/${action}`, {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Zhuorui-Action": "1",
        "X-CSRF-Token": state.csrfToken,
      },
      body: "{}",
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    if (payload.status) renderStatus(payload.status);
    if (!response.ok || !payload.ok) throw new Error(payload.message || `Action failed (${response.status}).`);
    toast(payload.message || "Control action completed.");
  } catch (error) {
    toast(error.message || "Control action failed.", true);
  } finally {
    state.busyAction = null;
    button.innerHTML = originalLabel;
    setButtonState();
  }
}

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", () => performAction(button));
});

elements["refresh-status"].addEventListener("click", () => fetchStatus(true));

elements["sign-out"].addEventListener("click", async () => {
  elements["sign-out"].disabled = true;
  try {
    await fetch("/api/logout", {
      method: "POST",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrfToken,
      },
      body: "{}",
    });
  } finally {
    window.location.replace("/login");
  }
});

async function initialize() {
  try {
    const response = await fetch("/api/session", { cache: "no-store" });
    if (!response.ok) {
      window.location.replace("/login");
      return;
    }
    const session = await response.json();
    state.csrfToken = session.csrf_token;
    await fetchStatus(false);
  } catch (_error) {
    toast("Could not establish a secure session.", true);
  }
}

state.clockTimer = window.setInterval(tick, 1000);
state.pollTimer = window.setInterval(() => fetchStatus(false), 60000);
tick();
initialize();
