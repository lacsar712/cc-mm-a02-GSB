const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const readingsView = document.querySelector("#view-readings");
const shiftsView = document.querySelector("#view-shifts");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const shiftMsg = document.querySelector("#shiftMsg");

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

function showView(name) {
  readingsView.hidden = name !== "readings";
  shiftsView.hidden = name !== "shifts";
  if (name === "shifts") {
    prefillOpenWindow();
    loadShifts();
  }
  if (name === "readings") load();
}

// 把 datetime-local 输入框预填为指定 Date（本地时区）
function setLocalInput(input, d) {
  const pad = (n) => String(n).padStart(2, "0");
  input.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function prefillOpenWindow() {
  const now = new Date();
  setLocalInput(document.querySelector("#shiftStart"), new Date(now.getTime() - 3600_000));
  setLocalInput(document.querySelector("#shiftEnd"), new Date(now.getTime() + 3600_000));
}

function showApp() {
  loginBox.hidden = true;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看（旁观）";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  document.querySelector("#openShiftForm").hidden = role !== "writer";
  connect();
  showView("readings");
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
    live.textContent = "上报成功";
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

document.querySelector("#nav-readings").onclick = () => showView("readings");
document.querySelector("#nav-shifts").onclick = () => showView("shifts");

// ---------- 班次窗 ----------

function fmt(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}

function localInputToIso(value) {
  return new Date(value).toISOString();
}

// ISO 串转 datetime-local 需要的本地时间分量串
function isoToLocalInput(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

async function loadShifts() {
  shiftMsg.textContent = "";
  try {
    const [shifts, events] = await Promise.all([
      api("/api/shifts"),
      api("/api/shift-events"),
    ]);
    paintShifts(shifts);
    paintEvents(events);
  } catch (err) {
    shiftMsg.textContent = err.message;
  }
}

let editingWindowId = null;

function paintShifts(list) {
  const canWrite = role === "writer";
  document.querySelector("#shiftRows").innerHTML = list
    .map((s) => {
      const open = s.status === "开启中";
      let actions = "";
      if (canWrite && open) {
        actions = `
          <span class="shift-actions">
            <button data-act="close" data-id="${s.id}">关闭</button>
            <button data-act="edit" data-id="${s.id}">改起止</button>
          </span>`;
      }
      let row = `<tr>
        <td>${s.name}</td>
        <td>${fmt(s.start_at)}</td>
        <td>${fmt(s.end_at)}</td>
        <td>${s.opened_by}</td>
        <td class="${open ? "ok" : "alarm"}">${s.status}${open ? "" : `（${s.closed_by} ${fmt(s.closed_at)}）`}</td>
        <td>${actions}</td>
      </tr>`;
      if (open && editingWindowId === s.id) {
        row += `<tr data-edit-row="${s.id}">
          <td colspan="6">
            新起刻 <input type="datetime-local" step="1" id="winStart-${s.id}" value="${isoToLocalInput(s.start_at)}" />
            新止刻 <input type="datetime-local" step="1" id="winEnd-${s.id}" value="${isoToLocalInput(s.end_at)}" />
            <button data-act="save-window" data-id="${s.id}">保存</button>
            <button data-act="cancel-window" data-id="${s.id}">取消</button>
          </td>
        </tr>`;
      }
      return row;
    })
    .join("");
}

function paintEvents(list) {
  const kindText = { 开启: "开启", 关闭: "关闭", 改窗: "改起止" };
  document.querySelector("#eventRows").innerHTML = list
    .map(
      (e) =>
        `<tr><td>${fmt(e.occurred_at)}</td><td>${e.shift_name}</td><td>${kindText[e.kind] || e.kind}</td><td>${e.actor}</td><td>${e.detail || ""}</td></tr>`,
    )
    .join("");
}

document.querySelector("#openShiftForm").onsubmit = async (e) => {
  e.preventDefault();
  const name = document.querySelector("#shiftName").value.trim();
  const start = document.querySelector("#shiftStart").value;
  const end = document.querySelector("#shiftEnd").value;
  if (!name || !start || !end) {
    shiftMsg.textContent = "班次名、起刻、止刻都必须填写";
    return;
  }
  try {
    await api("/api/shifts", {
      method: "POST",
      body: JSON.stringify({
        name,
        start_at: localInputToIso(start),
        end_at: localInputToIso(end),
      }),
    });
    shiftMsg.textContent = `班次「${name}」已开启`;
    document.querySelector("#shiftName").value = "";
    loadShifts();
  } catch (err) {
    shiftMsg.textContent = err.message;
  }
};

document.querySelector("#shiftRows").onclick = async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const id = btn.dataset.id;
  const act = btn.dataset.act;
  try {
    if (act === "close") {
      await api(`/api/shifts/${id}/close`, { method: "POST" });
      editingWindowId = null;
      shiftMsg.textContent = "班次已关闭，关闭事项已写入大事记";
      loadShifts();
    } else if (act === "edit") {
      editingWindowId = Number(id);
      loadShifts();
    } else if (act === "cancel-window") {
      editingWindowId = null;
      loadShifts();
    } else if (act === "save-window") {
      const start = document.querySelector(`#winStart-${id}`).value;
      const end = document.querySelector(`#winEnd-${id}`).value;
      if (!start || !end) {
        shiftMsg.textContent = "起止时刻都必须填写";
        return;
      }
      await api(`/api/shifts/${id}/window`, {
        method: "PATCH",
        body: JSON.stringify({
          start_at: localInputToIso(start),
          end_at: localInputToIso(end),
        }),
      });
      editingWindowId = null;
      shiftMsg.textContent = "起止时刻已修改";
      loadShifts();
    }
  } catch (err) {
    shiftMsg.textContent = err.message;
  }
};

if (token) showApp();
