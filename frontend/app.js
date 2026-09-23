const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const readingsView = document.querySelector("#readings-view");
const shiftsView = document.querySelector("#shifts-view");
const navShifts = document.querySelector("#nav-shifts");
const shiftMsg = document.querySelector("#shift-msg");
const shiftOpenBox = document.querySelector("#shift-open-box");
const shiftCurrentBox = document.querySelector("#shift-current-box");
let openShift = null;

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
    )
    .join("");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  navShifts.hidden = false;
  form.hidden = role !== "writer";
  connect();
  load();
}

async function load() {
  paint(await api("/api/readings"));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    load();
  };
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

function fmt(iso) {
  return new Date(iso).toLocaleString();
}

function toLocalInput(iso) {
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

navShifts.onclick = () => {
  const show = shiftsView.hidden;
  shiftsView.hidden = !show;
  readingsView.hidden = show;
  if (show) loadShiftPage();
};

async function loadShiftPage() {
  const [shifts, events] = await Promise.all([api("/api/shifts"), api("/api/shifts/events")]);
  openShift = shifts.find((s) => s.status === "开启中") || null;
  document.querySelector("#shift-rows").innerHTML = shifts
    .map(
      (s) =>
        `<tr><td>${s.name}</td><td>${fmt(s.start_at)}</td><td>${fmt(s.end_at)}</td><td>${s.opened_by}</td><td>${s.status}</td></tr>`,
    )
    .join("");
  document.querySelector("#event-rows").innerHTML = events
    .map((e) => `<tr><td>${e.shift_name}</td><td>${e.closed_by}</td><td>${fmt(e.closed_at)}</td></tr>`)
    .join("");
  const canWrite = role === "writer";
  shiftOpenBox.hidden = !(canWrite && !openShift);
  shiftCurrentBox.hidden = !(canWrite && openShift);
  if (canWrite && openShift) {
    document.querySelector("#shift-current").textContent =
      `${openShift.name}：${fmt(openShift.start_at)} ~ ${fmt(openShift.end_at)}`;
    document.querySelector("#edit-start").value = toLocalInput(openShift.start_at);
    document.querySelector("#edit-end").value = toLocalInput(openShift.end_at);
  }
}

document.querySelector("#shift-open-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/shifts", {
      method: "POST",
      body: JSON.stringify({
        name: document.querySelector("#shift-name").value,
        start_at: new Date(document.querySelector("#shift-start").value).toISOString(),
        end_at: new Date(document.querySelector("#shift-end").value).toISOString(),
      }),
    });
    document.querySelector("#shift-name").value = "";
    await loadShiftPage();
    shiftMsg.textContent = "班次已开启";
  } catch (err) {
    shiftMsg.textContent = err.message;
  }
};

document.querySelector("#shift-edit-form").onsubmit = async (e) => {
  e.preventDefault();
  if (!openShift) return;
  try {
    await api(`/api/shifts/${openShift.id}`, {
      method: "PUT",
      body: JSON.stringify({
        start_at: new Date(document.querySelector("#edit-start").value).toISOString(),
        end_at: new Date(document.querySelector("#edit-end").value).toISOString(),
      }),
    });
    await loadShiftPage();
    shiftMsg.textContent = "起止已保存";
  } catch (err) {
    shiftMsg.textContent = err.message;
  }
};

document.querySelector("#shift-close").onclick = async () => {
  if (!openShift) return;
  try {
    await api(`/api/shifts/${openShift.id}/close`, { method: "POST" });
    await loadShiftPage();
    shiftMsg.textContent = "班次已关闭，已写入大事记";
  } catch (err) {
    shiftMsg.textContent = err.message;
  }
};

if (token) showApp();
