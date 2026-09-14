"use strict";
const $ = (id) => document.getElementById(id),
  names = {
    collect: "数据采集",
    hil: "DAgger / HIL",
    inference: "模型推理",
    teleop: "遥操作",
  };
const phases = {
  hold: "已保持",
  human: "人工控制",
  policy: "模型执行",
  takeover: "冻结接管",
  resume: "准备交还",
  fault: "故障锁存",
};
let state = {},
  page = "workspace",
  arm = "left",
  online = false,
  busy = false,
  toastTimer,
  lastPoll = 0;
function text(id, value) {
  $(id).textContent = value;
}
function toast(message) {
  text("toast", message);
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 4500);
}
async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-YAM-Control": "1" },
    body: JSON.stringify(body || {}),
    signal: AbortSignal.timeout(5000),
  });
  const result = await response.json();
  if (!response.ok)
    throw Error(
      typeof result.detail === "string" ? result.detail : "请求参数无效",
    );
  return result;
}
async function action(path, body) {
  try {
    await post(path, body);
    await poll();
    return true;
  } catch (e) {
    toast(e.message);
    return false;
  }
}
function confirmAction(title, description, callback) {
  text("confirm-title", title);
  text("confirm-text", description);
  $("confirm-action").onclick = async () => {
    $("confirm-dialog").close();
    await callback();
  };
  $("confirm-dialog").showModal();
}
function activeDevice() {
  return (
    online &&
    state.connection === "connected" &&
    state.control_age_s != null &&
    state.control_age_s < 0.5 &&
    state.phase !== "fault"
  );
}
function camerasConnected() {
  return online && state.camera_connection === "connected";
}
function render() {
  const connected = activeDevice(),
    latched = !!state.stop_latched,
    maint = state.maintenance || "idle",
    paused = state.phase === "hold",
    mode = state.mode || "collect",
    recording = !!state.recording;
  const canRun =
      connected && !state.initializing && !latched && camerasConnected() && !!state.selected_task,
    canMaintain = connected && !latched && paused && !recording,
    idle = maint === "idle";
  text("environment", state.mock ? "模拟工作站" : "真实设备");
  $("environment").className = "pill" + (state.mock ? "" : " ok");
  const cNames = {
    disconnected: "设备未连接",
    connecting: "正在连接设备",
    connected: "设备已连接",
    disconnecting: "正在关闭设备",
    finalizing: "正在整理数据",
    fault: "会话失败",
  };
  text(
    "connection-text",
    online ? cNames[state.connection] || "会话运行中" : "界面连接中断",
  );
  $("connection-dot").className =
    "led" + (connected ? " ok" : state.connection === "fault" ? " bad" : "");
  const transitional = ["connecting", "disconnecting", "finalizing"].includes(
    state.connection,
  );
  $("connect").disabled =
    !online ||
    transitional ||
    (!state.selected_task?.task && state.connection !== "connected");
  renderTask(transitional || state.connection === "connected");
  const cameraBusy = ["connecting", "disconnecting"].includes(
    state.camera_connection,
  );
  $("connect-cameras").disabled = !online || cameraBusy;
  $("connect-cameras").querySelector("span").textContent = cameraBusy
    ? "相机处理中…"
    : camerasConnected()
      ? "断开相机"
      : "连接相机";
  $("connect").querySelector("span").textContent = transitional
    ? cNames[state.connection]
    : state.connection === "connected"
      ? "断开机械臂"
      : "连接机械臂";
  document.querySelectorAll("[data-mode]").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
    b.querySelector(".mode-state").textContent =
      b.dataset.mode === mode ? "● 当前模式" : "选择模式 →";
    b.disabled =
      !online || transitional || !!state.initializing || latched || state.phase === "fault";
  });
  text("mode-name", names[mode]);
  $("takeover").parentElement.hidden = mode !== "hil";
  $("task-instruction").title =
    state.selected_task?.instruction || "先选择任务";
  text(
    "phase",
    latched
      ? "紧急暂停"
      : maint === "homing"
        ? "正在回位"
        : maint === "gravity"
          ? "重力补偿"
          : connected
            ? phases[state.phase] || state.phase
            : "未就绪",
  );
  text(
    "control-hint",
    !connected
      ? "连接设备后准备开始"
      : latched
        ? "检查现场后解除暂停锁存"
        : maint === "homing"
          ? "回位独占控制 · 可随时暂停"
          : maint === "gravity"
            ? "手动摆放机械臂 · 结束后保持"
            : paused
              ? "等待开始指令"
              : state.phase === "human"
                ? "Leader 正在控制 Follower"
                : "本地策略控制中",
  );
  text(
    "start",
    mode === "collect" || mode === "teleop"
      ? "▶ 开始遥操作"
      : "▶ 开始模型执行",
  );
  $("start").disabled = !(canRun && paused && idle);
  $("header-stop").disabled =
    !online || state.connection !== "connected" || latched;
  $("header-reset").hidden = !latched;
  $("hold").disabled = !online || state.connection !== "connected";
  $("takeover").disabled = !(
    canRun &&
    mode === "hil" &&
    ["policy", "resume"].includes(state.phase)
  );
  $("resume").disabled = !(canRun && mode === "hil" && state.phase === "human");
  text(
    "handle-hint",
    mode === "collect"
      ? "手柄① 开始 / 结束录制　\n手柄② 放弃当前集"
      : mode === "hil"
        ? "键盘 I 冻结并接管　\n手柄① 交还模型 · ② 无功能"
        : "手柄按钮不分配功能",
  );
  text(
    "ownership",
    "控制权：" +
      (!connected
        ? "尚未连接"
        : latched
          ? "紧急暂停锁存"
          : maint === "homing"
            ? "回位控制"
            : maint === "gravity"
              ? "人工摆位 / 重力补偿"
              : paused
                ? "姿态保持"
                : state.source === "human"
                  ? "Leader 遥操作"
                  : "Thor 模型"),
  );
  $("record").disabled = !(
    canRun &&
    ["collect", "teleop"].includes(mode) &&
    (recording || state.phase === "human")
  );
  text("record", recording ? "■ 结束当前录制" : "● 开始录制");
  $("discard").disabled = !(
    canRun &&
    recording &&
    ["collect", "teleop"].includes(mode)
  );
  text("success", state.outcome === "success" ? "✓ 已标记成功" : "✓ 标记成功");
  text("failure", state.outcome === "failure" ? "已标记失败" : "标记失败");
  $("success").disabled = !canRun || !recording;
  $("failure").disabled = !canRun || !recording;
  text(
    "record-badge",
    recording
      ? "● 录制中 " + Math.floor(state.episode_elapsed_s || 0) + "s"
      : state.recording_saving ? "正在保存本集" : state.connection === "finalizing"
        ? "正在整理"
        : "未录制",
  );
  $("record-badge").className = "pill" + (recording ? " recording" : "");
  text("episode-count", String(state.episode_count || 0).padStart(2, "0"));
  text(
    "frame-count",
    (
      state.recorded_steps ??
      (state.episodes || []).reduce((sum, ep) => sum + ep.steps, 0)
    ).toLocaleString(),
  );
  text("interventions", state.intervention_id || 0);
  text(
    "disk",
    state.disk_free_gb == null ? "— GB" : state.disk_free_gb.toFixed(1) + " GB",
  );
  text(
    "output",
    state.output || "逐集保存 MP4＋HDF5 · 可上传服务器后独立转换",
  );
  text("latency", state.performance?.control_work?.p95_ms?.toFixed(2) + " ms");
  if (!connected) text("latency", "— ms");
  const skew = state.arrival_skew_s;
  text(
    "sync",
    connected && skew != null
      ? "相机到达偏差 " + (skew * 1000).toFixed(0) + " ms"
      : "尚无同步观测",
  );
  $("sync-dot").className =
    "led" + (connected && skew != null && skew < 0.12 ? " ok" : "");
  text(
    "preview-toggle",
    state.preview_enabled === false ? "开启预览" : "关闭预览",
  );
  for (const el of document.querySelectorAll("[data-camera]")) {
    const cam = (state.cameras || []).find((c) => c.role === el.dataset.camera);
    const valid =
      camerasConnected() && cam?.healthy && state.preview_enabled !== false;
    el.querySelector(".led").className =
      "led" + (camerasConnected() && cam?.healthy ? " ok" : "");
    el.querySelector(".camera-meta").textContent =
      cam && camerasConnected()
        ? `${cam.fps?.toFixed(0) || "—"} fps　 ·　帧龄 ${Math.round(cam.age_s * 1000)} ms`
        : "— fps　 ·　帧龄 —";
    if (!valid) {
      el.querySelector("img").hidden = true;
      el.querySelector(".camera-empty").hidden = false;
      el.querySelector(".camera-empty span").textContent =
        state.preview_enabled === false
          ? "预览已关闭，采集不受影响"
          : camerasConnected()
            ? "等待新鲜画面"
            : "等待相机连接";
    }
  }
  const ages = state.sdk_state_age_s || [];
  const devices = [
    ["左 Follower", 0],
    ["右 Follower", 2],
    ["左 Leader", 1],
    ["右 Leader", 3],
  ];
  $("health").replaceChildren();
  for (const [label, i] of devices) {
    const ok = connected && Number.isFinite(ages[i]) && ages[i] < 0.25;
    healthRow(
      label,
      ok
        ? Math.round(ages[i] * 1000) + " ms"
        : connected
          ? "反馈异常"
          : "未连接",
      ok,
    );
  }
  const healthy = (state.cameras || []).filter((c) => c.healthy).length;
  healthRow(
    "三路相机",
    camerasConnected() ? `${healthy} / 3 在线` : "未连接",
    camerasConnected() && healthy === 3,
  );
  healthRow(
    "Thor 模型",
    state.policy_configured
      ? state.mock
        ? "模拟策略"
        : state.source === "policy" && connected
          ? "策略执行中"
          : "已配置 / 待验证"
      : "未配置",
    state.source === "policy" && connected,
  );
  healthRow(
    "录制队列",
    connected ? `${state.record_queue || 0} 帧等待` : "未启动",
    connected && !state.error,
  );
  const errors = [
    !online ? "界面连接中断。工作台心跳超时将请求暂停；请检查现场状态。" : null,
    state.connection_error,
    state.camera_error,
    state.task_error,
    state.error,
    state.cleanup_error,
    state.maintenance_error,
    state.operator_error,
    state.operator_lost
      ? "操作台失联已触发暂停；重新连接不会自动恢复运动。"
      : null,
  ];
  const error = errors.filter(Boolean).join(" · ");
  text("alert", error);
  $("alert").hidden = !error;
  $("episodes").replaceChildren();
  const episodes = state.episodes || [];
  if (!episodes.length) {
    $("episodes").className = "empty-row";
    $("episodes").textContent = "还没有完成的采集集。第一段示范，从这里开始。";
  } else {
    $("episodes").className = "";
    for (const ep of [...episodes].reverse()) {
      const row = document.createElement("div");
      row.className = "episode-row";
      const a = document.createElement("span"),
        b = document.createElement("span");
      a.textContent = ep.path;
      b.textContent = `${ep.steps} 帧 · ${{ discarded: "已放弃", aborted: "中断", success: "成功", failure: "失败", unknown: "已结束" }[ep.outcome] || ep.outcome}`;
      row.append(a, b);
      $("episodes").append(row);
    }
  }
  $("logs").replaceChildren();
  for (const item of [...(state.events || [])].reverse().slice(0, 8)) {
    const row = document.createElement("div");
    row.className = "log-row";
    const t = document.createElement("time");
    t.textContent = item.time;
    row.append(t, document.createTextNode(item.message));
    $("logs").append(row);
  }
  $("device-cards").replaceChildren();
  for (const [name, i] of devices) {
    const card = document.createElement("article");
    card.className = "device-card";
    const title = document.createElement("h3"),
      meta = document.createElement("span");
    title.textContent = name;
    meta.textContent = connected
      ? `反馈 ${ages[i] == null ? "—" : Math.round(ages[i] * 1000)} ms · ${name.includes("Leader") ? "官方 YAM Leader" : "标准平行夹爪"}`
      : "设备未连接";
    card.append(title, meta);
    $("device-cards").append(card);
  }
  const q = state.follower_state || [],
    offset = arm === "left" ? 0 : 7;
  document.querySelectorAll("[data-joint-value]").forEach((el) => {
    const value = q[offset + Number(el.dataset.jointValue)];
    el.textContent =
      connected && value != null
        ? ((value * 180) / Math.PI).toFixed(1) + "°"
        : "—";
  });
  document
    .querySelectorAll(".jog-button")
    .forEach(
      (el) => (el.disabled = !(canMaintain && idle && mode === "collect")),
    );
  text(
    "gripper-value",
    connected && q[offset + 6] != null
      ? "当前开度 " + Math.round(q[offset + 6] * 100) + "%"
      : "开度 —",
  );
  $("capture-home").disabled = !(canMaintain && idle);
  $("home").disabled = !(canMaintain && idle && state.home_available);
  $("gravity").disabled = !(canMaintain && idle);
  $("gravity-exit").disabled = !(connected && !latched && maint === "gravity");
  text(
    "home-status",
    maint === "homing"
      ? "正在回准备位，完成后保持不动"
      : maint === "gravity"
        ? "重力补偿中：请手扶机械臂调整姿态"
        : state.home_available
          ? "准备位已保存 · 回位前请清空完整运动路径"
          : "尚未保存准备位",
  );
  renderInitialization({ connected, canMaintain, idle, maint, transitional });
  text("clock", new Date().toLocaleTimeString("zh-CN", { hour12: false }));
}
let initInventorySignature = "";
function renderInitialization(context) {
  const init = state.initialization || {},
    inventory = init.inventory || {},
    preflight = init.preflight,
    accepted = init.accepted,
    cameras = state.cameras || [],
    cameraDone =
      camerasConnected() && cameras.length === 3 && cameras.every((c) => c.healthy),
    ages = state.sdk_state_age_s || [],
    armDone = context.connected && ages.length === 4 && ages.every((x) => x < 0.25),
    gravityDone = $("init-gravity-check").checked && $("init-leader-check").checked;

  const signature = JSON.stringify(inventory);
  if (signature !== initInventorySignature) {
    initInventorySignature = signature;
    $("init-inventory").replaceChildren();
    const labels = [
      ...(inventory.followers || []).map(
        (d) => `${d.side} follower · ${d.channel} · ${d.gripper}`,
      ),
      ...(inventory.leaders || []).map(
        (d) => `${d.side} leader · ${d.channel} · ${d.gripper}`,
      ),
      ...(inventory.cameras || []).map(
        (d) => `${d.role} camera · ${d.serial || "未填序列号"}`,
      ),
    ];
    for (const label of labels) {
      const chip = document.createElement("span");
      chip.textContent = label;
      $("init-inventory").append(chip);
    }
  }

  const done = [!!preflight?.ok, cameraDone, armDone, gravityDone, !!accepted];
  ["preflight", "cameras", "arms", "gravity", "complete"].forEach((name, i) => {
    $("init-step-" + name).classList.toggle("done", done[i]);
    $("init-step-" + name).classList.toggle(
      "active",
      !done[i] && done.slice(0, i).every(Boolean),
    );
  });
  text("init-progress", `${done.filter(Boolean).length} / 5`);
  text(
    "init-preflight-status",
    preflight
      ? preflight.ok
        ? `预检通过 · ${preflight.can?.length || 0} 路 CAN · ${preflight.camera_serials?.length || 0} 台相机`
        : `发现问题：${(preflight.errors || []).join("；")}`
      : "检查 4 路 CAN、夹爪型号和 3 台相机序列号",
  );
  text(
    "init-camera-status",
    cameraDone
      ? "三路画面均在线且帧龄正常"
      : state.camera_connection === "connecting"
        ? "正在连接并等待新鲜画面"
        : "连接 top / left / right 并确认画面新鲜",
  );
  const limits = state.gripper_limits || [];
  text(
    "init-arm-status",
    armDone
      ? `四臂反馈正常 · 夹爪行程 ${limits.map((x) => x?.map((v) => Number(v).toFixed(3)).join(" → ") || "—").join(" / ")}`
      : state.connection === "connecting"
        ? "正在顺序连接设备；未固定行程的夹爪会先完成自动标定"
        : "夹爪先闭合并清空行程；连接后保持当前位置",
  );
  text(
    "init-complete-status",
    accepted
      ? `上次验收：${accepted.accepted_at || "已保存"}`
      : "保存配置指纹、设备清单和夹爪行程测量",
  );

  $("init-preflight").disabled = !online || context.transitional;
  $("init-cameras").disabled =
    !online || cameraDone || ["connecting", "disconnecting"].includes(state.camera_connection);
  $("init-arms").disabled =
    !online || !preflight?.ok || !cameraDone || state.connection !== "disconnected";
  $("init-gravity").disabled =
    context.maint === "gravity"
      ? !context.connected
      : !(context.canMaintain && context.idle);
  text(
    "init-gravity",
    context.maint === "gravity" ? "结束补偿并保持" : "进入重力补偿",
  );
  $("init-gravity-check").disabled = !armDone || context.maint === "gravity";
  $("init-leader-check").disabled = !armDone;
  $("init-complete").disabled = !(preflight?.ok && cameraDone && armDone && gravityDone);
}
function healthRow(label, value, ok) {
  const row = document.createElement("div");
  row.className = "health-row";
  const a = document.createElement("span"),
    b = document.createElement("span"),
    dot = document.createElement("i");
  a.textContent = label;
  dot.className = "led" + (ok ? " ok" : "");
  b.append(dot, document.createTextNode(value));
  row.append(a, b);
  $("health").append(row);
}
async function poll() {
  if (busy) return;
  busy = true;
  try {
    const r = await fetch("/status", { signal: AbortSignal.timeout(2000) });
    if (!r.ok) throw Error("状态读取失败");
    state = await r.json();
    online = true;
    lastPoll = Date.now();
  } catch (e) {
    online = false;
  } finally {
    busy = false;
    render();
  }
}
async function heartbeat() {
  try {
    await post("/heartbeat");
  } catch (e) {
    /* State polling handles visible errors. */
  }
}
function switchPage(next) {
  page = next;
  document
    .querySelectorAll("[data-page]")
    .forEach((b) => b.classList.toggle("active", b.dataset.page === next));
  $("workspace-page").hidden = next !== "workspace";
  $("devices-page").hidden = next !== "devices";
  text("page-name", next === "workspace" ? "采集工作台" : "设备与调试");
  text("heading", next === "workspace" ? "采集工作台" : "设备与调试");
  text(
    "subtitle",
    next === "workspace"
      ? "选择任务，连接设备，让每一段示范都有清晰的归属。"
      : "查看四臂状态，示教准备位，完成采集前的设备调试。",
  );
}
for (let j = 0; j < 6; j++) {
  const el = document.createElement("div");
  el.className = "joint";
  el.innerHTML = `<div class="joint-head"><span>关节 J${j + 1}</span><strong data-joint-value="${j}">—</strong></div><div class="joint-buttons"><button class="button jog-button" data-joint="${j}" data-delta="-1">−</button><button class="button jog-button" data-joint="${j}" data-delta="1">＋</button></div>`;
  $("joint-controls").append(el);
}
document
  .querySelectorAll("[data-page]")
  .forEach((b) => (b.onclick = () => switchPage(b.dataset.page)));
document
  .querySelectorAll("[data-mode]")
  .forEach((b) => (b.onclick = () => action("/event/mode:" + b.dataset.mode)));
document
  .querySelectorAll("[data-event]")
  .forEach((b) => (b.onclick = () => action("/event/" + b.dataset.event)));
document.querySelectorAll("[data-arm]").forEach(
  (b) =>
    (b.onclick = () => {
      arm = b.dataset.arm;
      document
        .querySelectorAll("[data-arm]")
        .forEach((x) => x.classList.toggle("active", x === b));
      render();
    }),
);
document.querySelectorAll("[data-joint]").forEach(
  (b) =>
    (b.onclick = () =>
      action("/jog", {
        arm,
        joint: Number(b.dataset.joint),
        delta: (Number(b.dataset.delta) * Math.PI) / 90,
      })),
);
$("gripper-open").onclick = () => action("/jog", { arm, joint: 6, delta: 0.1 });
$("gripper-close").onclick = () =>
  action("/jog", { arm, joint: 6, delta: -0.1 });
$("preview-toggle").onclick = () =>
  action(
    "/event/" +
      (state.preview_enabled === false ? "preview_on" : "preview_off"),
  );
$("header-stop").onclick = () => action("/event/stop");
$("header-reset").onclick = () =>
  confirmAction(
    "解除软件暂停锁存？",
    "确认现场已排除异常。解除后保持不动，不恢复旧动作；你可以再选择开始、回准备位或重力补偿。",
    () => action("/event/reset_stop"),
  );
$("capture-home").onclick = () =>
  confirmAction(
    "保存当前四臂准备位？",
    "这将覆盖本机当前配置的准备位。请确认四台机械臂姿态合适、Leader 与 Follower 对齐；保存不会运动。",
    () => action("/event/capture_home"),
  );
$("home").onclick = () =>
  confirmAction(
    "回到准备位？",
    "回位将独占四臂控制，并沿关节插值路径运动。请确认完整路径没有人员或障碍；夹爪保持当前开度。可随时按暂停。",
    () => action("/event/home"),
  );
$("gravity").onclick = () =>
  confirmAction(
    "进入重力补偿？",
    "请手扶机械臂后继续。模型和遥操作将停止；四臂关节可手动摆放，Follower夹爪保持。完成后点击“结束补偿 / 保持”。",
    () => action("/event/gravity"),
  );
$("init-preflight").onclick = async () => {
  try {
    const result = await post("/initialize/preflight");
    await poll();
    toast(result.ok ? "只读预检通过，可以连接相机" : (result.errors || []).join("；"));
  } catch (error) {
    toast(error.message);
  }
};
$("init-cameras").onclick = () => action("/cameras/connect");
$("init-arms").onclick = () =>
  confirmAction(
    "开始四臂初始化？",
    "请确认两只 Follower 夹爪已手动闭合、夹爪行程无障碍，四台机械臂已固定，手柄按钮全部释放且有人照看。连接可能施加力矩并执行夹爪标定，但不会自动回零或启动遥操作。",
    () => action("/connect", { ready: true, initialize: true }),
  );
$("init-gravity").onclick = () => {
  if (state.maintenance === "gravity") return action("/event/hold");
  confirmAction(
    "进入初始化重力补偿测试？",
    "请手扶机械臂后继续。轻推两台 Follower 并观察松手后是否基本停留；同时检查两台 Leader 是否顺滑。完成后点击“结束补偿并保持”。",
    () => action("/event/gravity"),
  );
};
$("init-gravity-check").onchange = render;
$("init-leader-check").onchange = render;
$("init-complete").onclick = async () => {
  try {
    await post("/initialize/complete", {
      gravity_checked: $("init-gravity-check").checked,
      leader_checked: $("init-leader-check").checked,
    });
    await poll();
    toast("设备初始化验收已保存；现在可以结束补偿、保存准备位或进入采集工作台");
  } catch (error) {
    toast(error.message);
  }
};
$("discard").onclick = () =>
  confirmAction(
    "放弃当前这一集？",
    "当前集将停止写入并删除，之前已保存的集不受影响。遥操作继续。",
    () => action("/event/discard"),
  );
$("connect").onclick = () => {
  if (state.connection === "connected") {
    confirmAction(
      "断开机械臂并保存会话？",
      "结束控制可能使机械臂失去支撑，请先支撑四台机械臂。结束后保存MP4与HDF5数据；未结束的采集集按中断处理。",
      () => action("/disconnect", { supported: true }),
    );
  } else {
    text(
      "connect-task-summary",
      "当前任务：" + (state.selected_task?.name || "请先选择任务"),
    );
    $("real-warning").hidden = !!state.mock;
    $("connect-dialog").showModal();
  }
};
$("connect-form").onsubmit = async (e) => {
  e.preventDefault();
  const payload = { ready: true, initialize: false };
  if ($("url-input").value.trim()) payload.url = $("url-input").value.trim();
  $("connect-dialog").close();
  await action("/connect", payload);
};
document
  .querySelectorAll("[data-close]")
  .forEach((b) => (b.onclick = () => b.closest("dialog").close()));
// Keyboard remains available on both pages; never intercept typing or open dialogs.
document.addEventListener("keydown", (e) => {
  if (
    e.repeat ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) ||
    document.querySelector("dialog[open]")
  )
    return;
  const event = {
    i: "takeover",
    " ": "hold",
    s: "start",
    r: "record",
    x: "discard",
    g: "success",
    f: "failure",
    1: "mode:teleop",
    2: "mode:inference",
    3: "mode:hil",
    4: "mode:collect",
  }[e.key.toLowerCase()];
  if (event) {
    e.preventDefault();
    if (event === "discard") $("discard").click();
    else action("/event/" + event);
  }
});
for (const img of document.querySelectorAll(".camera img")) {
  img.onload = () => {
    delete img.dataset.loading;
    if (camerasConnected() && state.preview_enabled !== false) {
      img.hidden = false;
      img.nextElementSibling.hidden = true;
    }
  };
  img.onerror = () => {
    delete img.dataset.loading;
    img.hidden = true;
    img.nextElementSibling.hidden = false;
  };
}
setInterval(() => {
  if (
    page !== "workspace" ||
    !camerasConnected() ||
    state.preview_enabled === false ||
    document.hidden
  )
    return;
  for (const img of document.querySelectorAll(".camera img")) {
    if (!img.dataset.loading) {
      img.dataset.loading = "1";
      img.src =
        "/camera/" + img.parentElement.dataset.camera + ".jpg?t=" + Date.now();
    }
  }
}, 250);
setInterval(poll, 400);
setInterval(heartbeat, 1000);
heartbeat();
poll();

function renderTask(locked) {
  const task = state.selected_task;
  text("task-library-count", `${(state.tasks || []).length} 个任务`);
  text("task-badge", task ? "当前任务" : "尚未选择");
  text("task-category", "任务 / DATASET TASK");
  text("task-name", task?.name || "从一个采集任务开始");
  text(
    "task-instruction",
    task?.instruction || "例如创建“乐高分拣”，为示范数据设置明确的任务指令。",
  );
  text(
    "task-identity",
    task
      ? `任务 ID · ${task.id.slice(0, 8)}`
      : "任务名称与指令会随数据一起保存",
  );
  text(
    "task-session-tip",
    locked
      ? "任务已锁定 · 断开机械臂并完成保存后可切换"
      : "同一任务的每次采集会话独立保存",
  );
  $("create-task").disabled = !online || locked;
  $("edit-task").disabled = !online || locked || !task;
  text(
    "task-english",
    "task: " + (task?.task || "待补填英文 task，请点击编辑任务"),
  );
  $("choose-task").disabled = !online || locked || !state.tasks?.length;
  $("step-task").classList.toggle("done", !!task);
  $("step-devices").classList.toggle(
    "done",
    activeDevice() && camerasConnected(),
  );
  $("step-record").classList.toggle("done", !!state.recording);
  text(
    "workflow-tip",
    !task
      ? "先创建或选择任务"
      : !task.task
        ? "请在任务详情中编辑并补填英文 task"
        : !camerasConnected()
          ? "连接三路相机，检查画面"
          : !activeDevice()
            ? "连接机械臂，完成准备"
            : state.recording
              ? "正在录制 · 数据归入当前任务"
              : "设备就绪，选择模式后开始",
  );
}
$("connect-cameras").onclick = () =>
  action(camerasConnected() ? "/cameras/disconnect" : "/cameras/connect");
$("create-task").onclick = () => {
  editingTask = null;
  $("task-form").reset();
  text("task-dialog-title", "创建采集任务");
  text("task-submit", "创建并选择任务");
  text("task-form-error", "");
  $("task-dialog").showModal();
};
$("task-form").onsubmit = async (e) => {
  e.preventDefault();
  $("task-submit").disabled = true;
  try {
    await post(editingTask ? `/tasks/${editingTask}/update` : "/tasks", {
      name: $("new-task-name").value.trim(),
      instruction: $("new-task-instruction").value.trim(),
      task: $("new-task-english").value.trim(),
    });
    $("task-dialog").close();
    $("task-form").reset();
    await poll();
    toast("任务已保存并选中");
  } catch (error) {
    text("task-form-error", error.message);
  } finally {
    $("task-submit").disabled = false;
  }
};
$("choose-task").onclick = () => {
  $("task-options").replaceChildren();
  for (const task of state.tasks || []) {
    const button = document.createElement("button");
    button.className = "task-option";
    const title = document.createElement("strong"),
      detail = document.createElement("span");
    title.textContent = task.name;
    detail.textContent = task.instruction;
    button.append(title, detail);
    button.onclick = async () => {
      if (await action(`/tasks/${task.id}/select`)) $("task-picker").close();
    };
    $("task-options").append(button);
  }
  $("task-picker").showModal();
};

$("expand-vision").onclick = () => {
  const expanded = $("workspace-page").classList.toggle("vision-expanded");
  $("expand-vision").setAttribute("aria-pressed", String(expanded));
  text("expand-vision", expanded ? "恢复布局" : "放大视觉区");
};

let editingTask = null;
$("edit-task").onclick = () => {
  const task = state.selected_task;
  if (!task) return;
  editingTask = task.id;
  $("new-task-name").value = task.name;
  $("new-task-instruction").value = task.instruction;
  $("new-task-english").value = task.task || "";
  text("task-dialog-title", "编辑采集任务");
  text("task-submit", "保存任务");
  text("task-form-error", "");
  $("task-dialog").showModal();
};

$("fullscreen").onclick = async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else if (document.documentElement.requestFullscreen)
      await document.documentElement.requestFullscreen();
    else toast("当前浏览器不支持页面全屏，请最大化窗口");
  } catch {
    toast("当前浏览器未允许页面全屏，请最大化窗口");
  }
};
document.addEventListener("fullscreenchange", () => {
  text("fullscreen", document.fullscreenElement ? "退出全屏" : "全屏");
});
