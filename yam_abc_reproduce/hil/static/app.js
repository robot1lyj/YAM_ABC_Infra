"use strict";
const controlSession = sessionStorage.getItem("yam-control-session") ||
  (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
sessionStorage.setItem("yam-control-session", controlSession);
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
  takeover: "介入待接管 · 随时按右 Leader ①开启遥操作",
  resume: "准备交还",
  fault: "故障锁存",
};
let state = {},
  page = "workspace",
  arm = "left",
  online = false,
  busy = false,
  policyDraft = null,
  toastTimer,
  lastPoll = 0;
function text(id, value) {
  $(id).textContent = value;
}
function operatorHint(message) {
  const raw = String(message || "");
  const rules = [
    [/Ownership retained|SDK startup failed with uncertain cleanup/i, "机械臂SDK初始化失败，尚不能确认后台控制线程已清理。为避免两个进程同时控制，CAN所有权仍保留；请支撑机械臂、保留日志后受控重启设备持有进程，不要反复连接或Reset CAN。"],
    [/CAN .* is owned by/i, "CAN正被另一设备会话占用。请确认是哪一个控制进程，支撑机械臂后正常关闭原会话，再连接；不要直接重置正在使用的总线。"],
    [/fail to communicate with the motor/i, "电机通信失败。请检查提示中的CAN通道、控制器供电和USB-CAN状态；先暂停并支撑机械臂，不要连续重试。若同时提示SDK清理不确定，需要受控重启设备持有进程。"],
    [/Required data filesystem|Recording filesystem|Recording path|Recording directory|YAM_RECORDING_/i, "录制存储未就绪。请检查数据盘是否正确挂载、是否只读、目录权限和剩余空间；修复后在保持状态点击恢复录制服务，不需要重连机械臂。"],
    [/RTC committed target changed at actuation/i, "RTC承诺动作与实际下发目标不一致。请保持暂停并保留诊断信息，检查动作下发链路，不要连续重试。这不是两臂实测姿态偏差，回零不一定能修复。"],
    [/SDK state update stale/i, "机械臂状态反馈超时。请先暂停，检查控制器供电、USB-CAN连接及总线状态；确认机械臂已支撑后再断开重连。反复出现时请保留日志排查，不要反复启动运动。"],
    [/CAN interface.*not up|CAN setup failed/i, "CAN接口未正常启动。请检查USB-CAN连接及控制器供电；确认四臂已支撑并断开会话后，使用Reset CAN恢复，再尝试连接。"],
    [/episode queue full|recording queue full|encoder.*queue.*full/i, "录制处理队列已满。当前集可能不完整，请保持暂停并检查磁盘空间和编码器状态；排除原因后点击恢复录制服务，旧数据会保留，不需要重连机械臂。"],
    [/invalid policy response/i, "模型返回的动作格式或数值无效。请暂停，检查Thor服务协议、动作形状和有限数值；修复后重载推理通信或动作规划，再手动开始，不需要重连机械臂。"],
    [/Replay refused.*pose/i, "回放起始姿态与当前Follower姿态不一致。请暂停并核对回放起点；只有起点确为零位时才使用回零，然后重新开始。"],
    [/No space left|disk.*full/i, "存储空间不足。请停止录制，等待保存结束，备份并清理不需要的数据后再录制。"],
    [/timed? ?out|TimeoutError|Failed to fetch|NetworkError|connection refused/i, "服务连接失败或响应超时。请检查网络和对应服务状态，再刷新确认；恢复连接不会自动恢复运动。"],
  ];
  const match = rules.find(([pattern]) => pattern.test(raw));
  if (match) return `${match[1]}\n原始诊断：${raw}`;
  if (raw && !/[\u3400-\u9fff]/.test(raw))
    return `操作未完成。请保留以下诊断信息，检查对应服务状态；涉及运动时先暂停，不要连续重试。\n原始诊断：${raw}`;
  return raw;
}
function toast(message) {
  text("toast", operatorHint(message));
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 4500);
}
async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-YAM-Control": "1",
      "X-YAM-Session": controlSession,
      "X-YAM-Visible": document.visibilityState === "visible" ? "1" : "0" },
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
    reportedMode = state.mode || "collect",
    mode = !state.selected_task && !state.initializing ? "teleop" : reportedMode,
    recording = !!state.recording;
  const teleopView = mode === "teleop" || !state.selected_task,
    collectionReady = camerasConnected() && !!state.selected_task,
    canBindTask =
      connected && !state.task_switching && !state.initializing && !state.intervention_pending && paused && !latched &&
      maint === "idle" && !recording && !state.recording_saving &&
      !state.recording_error,
    canRun =
      connected && !state.task_switching && !state.initializing && !latched &&
      (mode === "teleop" || (collectionReady && !state.recording_error &&
        (!["inference", "hil"].includes(mode) || (state.policy_ready && !state.policy_error)))),
    canRecord =
      connected &&
      !state.initializing &&
      !latched &&
      !state.recording_error &&
      collectionReady &&
      mode === "collect",
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
    !online || transitional;
  $("connect").title =
    state.connection === "connected"
      ? "断开机械臂并保存当前会话"
      : state.selected_task?.task
        ? "连接当前任务的四台机械臂；连接后先保持"
        : "无需采集任务，直接连接四臂进行遥操作";
  renderTask(transitional || (state.connection === "connected" && !canBindTask));
  const cameraBusy = ["connecting", "disconnecting"].includes(
    state.camera_connection,
  );
  $("connect-cameras").disabled = !online || cameraBusy;
  $("connect-cameras").querySelector("span").textContent = cameraBusy
    ? "相机处理中…"
    : state.camera_cleanup_pending
      ? "重试断开相机"
    : camerasConnected()
      ? "断开相机"
      : teleopView
        ? "连接相机（可选）"
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
      !online ||
      transitional ||
      !!state.task_switching ||
      !!state.initializing ||
      latched ||
      state.phase === "fault" ||
      (state.recording_error && b.dataset.mode !== "teleop") ||
      (!state.selected_task && b.dataset.mode !== "teleop");
    b.title = state.initializing
      ? "当前为设备初始化会话；完成或退出向导后开放工作模式"
      : !state.selected_task && b.dataset.mode !== "teleop"
        ? "请先新建或选择采集任务"
        : "";
  });
  $("workspace-page").classList.toggle("teleop-view", teleopView);
  $("recording-controls").hidden = teleopView || mode !== "collect";
  const recoveringRecording = state.recording_recovery?.state === "recovering";
  $("recording-restart").hidden = !state.recording_error && !recoveringRecording;
  $("recording-restart").disabled = !connected || !paused || recording ||
    !idle || latched || state.intervention_pending || state.task_switching || recoveringRecording;
  text("recording-restart", recoveringRecording ? "录制服务恢复中…" : "恢复录制服务");
  $("recording-restart").title = "保留故障数据并创建新数据会话，不断开机械臂、不自动开始运动";
  $("policy-panel").hidden = !["inference", "hil"].includes(mode);
  text("policy-mode", state.policy_fusion === "tda_smooth" ? "TDA 推理" : state.policy_fusion === "sync_hold" ? (state.policy_waiting_for_reply ? "同步推理 · 保持" : "同步推理") : state.policy_fusion === "rtc" ? "RTC 推理" : "旧模式");
  text("policy-rtt", state.policy_observed_rtt_p95_s == null ? "—" : `${Math.round(state.policy_observed_rtt_p95_s * 1000)} ms`);
  text("policy-buffer", state.policy_buffer_seconds == null ? "—" : `${Math.max(0, state.policy_buffer_seconds).toFixed(2)} s`);
  text("policy-trim", state.policy_fusion === "rtc"
    ? `${state.rtc_delay_steps ?? "—"} 步前缀`
    : state.policy_trimmed_steps == null ? "—" : `${state.policy_trimmed_steps} 步`);
  text("policy-speed", state.policy_trajectory_active
    ? `${state.policy_trajectory_hz} Hz 二阶` : "30 Hz 直达 SDK");
  if (policyDraft && state.policy_fusion === policyDraft.fusion &&
      (policyDraft.fusion !== "rtc" || state.rtc_delay_steps === policyDraft.rtc_delay_steps)) policyDraft = null;
  if (!policyDraft) {
    $("policy-fusion").value = state.policy_fusion || "tda_smooth";
    $("rtc-delay-steps").value = state.rtc_delay_steps ?? 9;
  }
  const policyEditable = connected && !state.task_switching && paused && !latched && !recording && maint === "idle";
  const sourceEditable = online && !state.initializing && !state.mock &&
    (state.connection === "disconnected" || policyEditable);
  $("policy-fusion").disabled = !policyEditable;
  $("rtc-delay-steps").disabled = !policyEditable || $("policy-fusion").value !== "rtc";
  $("policy-apply").disabled = !policyEditable;
  $("policy-restart").disabled = !policyEditable || !!state.mock;
  $("policy-source-apply").disabled = !sourceEditable;
  $("policy-source-url").disabled = !sourceEditable;
  $("policy-source-local").disabled = !sourceEditable;
  $("interaction-reload").disabled = !policyEditable;
  $("policy-source-thor").disabled = !sourceEditable;
  if (!$("policy-source-url").value) $("policy-source-url").value = state.policy_url || "";
  text("policy-source-current", `当前来源：${state.policy_url || "未配置"}`);
  $("planner-restart").disabled = !policyEditable || !!state.mock;
  $("session-summary").hidden = teleopView;
  $("recent-episodes").hidden = teleopView;
  text("control-title", teleopView ? "遥操作控制" : "采集控制");
  text(
    "heading",
    page === "workspace"
      ? teleopView
        ? "遥操作"
        : "采集工作台"
      : "设备与调试",
  );
  text("mode-name", names[mode]);
  $("takeover").parentElement.hidden = mode !== "hil";
  $("task-instruction").title =
    state.selected_task?.instruction || "遥操作无需任务；录制前再选择采集任务";
  text(
    "phase",
    latched
      ? "紧急暂停"
      : maint === "homing"
        ? "正在回位"
        : maint === "gravity"
          ? "重力补偿"
          : connected
            ? (state.phase === "hold" && state.leader_locked ? "已锁定 · 等待页面交还" : phases[state.phase] || state.phase)
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
              ? state.recording_error
                ? "录制失败，已保持；排除存储或编码原因后可恢复录制服务"
                : "等待开始指令"
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
  const intervening = mode === "hil" && (state.intervention_pending || ["takeover", "human"].includes(state.phase));
  $("start").disabled = !(canRun && paused && idle) || intervening;
  $("start").title = ["inference", "hil"].includes(mode) && !state.policy_ready
    ? "推理通信尚未就绪；可在保持状态重载推理通信" : "";
  if (intervening) $("start").title = "介入期间只能暂停或明确交还模型";
  $("header-stop").disabled =
    online && state.connection === "disconnected";
  $("header-reset").hidden = !latched;
  $("hold").disabled = online && state.connection === "disconnected";
  $("takeover").disabled = !(
    canRun &&
    mode === "hil" &&
    ["policy", "resume"].includes(state.phase)
  );
  $("resume").disabled = !(canRun && mode === "hil" && (intervening || (paused && state.leader_locked)));
  text(
    "handle-hint",
    teleopView
      ? "遥操作不录制　\nLeader 按钮仅做输入状态检查"
      : mode === "collect"
      ? "手柄① 开始 / 结束录制　\n手柄② 放弃当前集"
      : mode === "hil"
        ? "I 介入锁定 → 右①遥操作 → ①锁定待交还　\n点击交还模型继续 · ② 无功能"
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
                  : state.policy_waiting_for_reply
                    ? "等待 Thor · 姿态保持"
                  : state.policy_trajectory_active
                    ? `Thor 模型 / ${state.policy_trajectory_hz} Hz 二阶轨迹`
                    : "Thor 模型"),
  );
  $("record").disabled = !(
    canRecord &&
    ["collect", "teleop"].includes(mode) &&
    (recording || state.phase === "human")
  );
  text("record", recording ? "■ 结束当前录制" : "● 开始录制");
  $("discard").disabled = !(
    canRecord &&
    recording &&
    ["collect", "teleop"].includes(mode)
  );
  text("success", state.outcome === "success" ? "✓ 已标记成功" : "✓ 标记成功");
  text("failure", state.outcome === "failure" ? "已标记失败" : "标记失败");
  $("success").disabled = !canRecord || !recording;
  $("failure").disabled = !canRecord || !recording;
  text(
    "record-badge",
    recording
      ? "● 录制中 " + Math.floor(state.episode_elapsed_s || 0) + "s"
      : state.recording_error ? "录制中断 · 已保持"
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
    const img = el.querySelector("img");
    const previewStale =
      Date.now() - Number(img.dataset.lastGoodAt || 0) > 2000 ||
      (state.preview_age_s != null && state.preview_age_s > 2);
    if (!valid &&
        (!camerasConnected() || state.preview_enabled === false || previewStale)) {
      img.hidden = true;
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
  if (!teleopView) {
    if (state.policy_command) {
      const commandState = state.policy_command.state;
      healthRow("设置请求", {
        queued: "等待控制确认",
        accepted: "控制已接收",
        rejected: "未应用 · 见提示",
      }[commandState] || "待确认", commandState === "accepted");
    }
    healthRow(
      "Thor 模型",
      state.policy_configured
        ? state.mock
          ? "模拟策略"
          : state.policy_restart_error
            ? "推理通信故障"
            : state.source === "policy" && connected
              ? "策略执行中"
            : state.policy_ready
              ? "推理通信就绪"
            : "已配置 / 待验证"
        : "未配置",
      state.source === "policy" && connected,
    );
    healthRow(
      "动作规划",
      state.planner_ready ? "独立进程就绪" : "未就绪 / 保持",
      !!state.planner_ready,
    );
    healthRow(
      "录制队列",
      state.recording_error
        ? "录制中断"
        : state.recording_saving
          ? `正在整理 · ${((state.record_metrics?.encoder?.spool_bytes || 0) / 1048576).toFixed(0)} MB 待编码`
          : recording
            ? `${state.record_queue || 0} 帧暂存 · ${((state.record_metrics?.encoder?.spool_bytes || 0) / 1048576).toFixed(0)} MB 待编码`
            : "未录制",
      connected && !state.error && !state.recording_error,
    );
  }
  const errors = [
    !online ? "界面连接中断。工作台心跳超时将请求暂停；请检查现场状态。" : null,
    state.connection_error,
    state.camera_error,
    state.task_error,
    state.error,
    state.recording_error,
    state.recording_recovery?.error,
    ["inference", "hil"].includes(mode) ? state.policy_error : null,
    state.cleanup_error,
    maint !== "idle" ? state.maintenance_error : null,
    state.operator_error,
    state.leader_alignment_error,
    state.policy_wait_reason,
    ["inference", "hil"].includes(mode) ? state.policy_restart_error : null,
    state.policy_command?.state === "rejected" ? state.policy_command.error : null,
    state.health_error ? `健康采样暂不可用：${state.health_error}` : null,
    state.recording_storage?.ready === false
      ? `录制存储不可用，请检查数据盘挂载、权限和空间：${(state.recording_storage.errors || []).join("；")}`
      : null,
    state.operator_lost
      ? "操作台失联已触发暂停；重新连接不会自动恢复运动。"
      : null,
  ];
  const error = errors.filter(Boolean).map(operatorHint).join(" · ");
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
      meta = document.createElement("span"),
      dot = document.createElement("i"),
      ok = connected && Number.isFinite(ages[i]) && ages[i] < 0.25,
      pose = name.includes("Follower")
        ? state.follower_state || []
        : state.leader_state || [],
      poseOffset = name.startsWith("左") ? 0 : 7,
      angles = document.createElement("div");
    dot.className = "led" + (ok ? " ok" : "");
    title.append(dot, document.createTextNode(name));
    meta.textContent = connected
      ? `反馈 ${ages[i] == null ? "—" : Math.round(ages[i] * 1000)} ms`
      : "设备未连接";
    angles.className = "arm-angles";
    for (let joint = 0; joint < 6; joint++) {
      const item = document.createElement("div"),
        label = document.createElement("small"),
        value = document.createElement("strong"),
        radians = pose[poseOffset + joint];
      label.textContent = `J${joint + 1}`;
      value.textContent =
        connected && Number.isFinite(radians)
          ? `${((radians * 180) / Math.PI).toFixed(1)}°`
          : "—";
      item.append(label, value);
      angles.append(item);
    }
    card.append(title, meta, angles);
    $("device-cards").append(card);
  }
  const q = state.follower_state || [],
    offset = arm === "left" ? 0 : 7;
  document.querySelectorAll("[data-joint-value]").forEach((el) => {
    const value = q[offset + Number(el.dataset.jointValue)];
    const degrees = value == null ? null : (value * 180) / Math.PI;
    el.textContent =
      connected && degrees != null ? degrees.toFixed(1) + "°" : "—";
    const slider = document.querySelector(
      `[data-joint-slider="${el.dataset.jointValue}"]`,
    );
    if (slider) slider.value = connected && degrees != null ? degrees : 0;
  });
  document
    .querySelectorAll(".jog-button")
    .forEach(
      (el) => {
        el.disabled = !(canMaintain && idle && !intervening);
        el.title = el.disabled ? "需连接、暂停、结束录制/介入并退出维护；无需切换模式" : "小步关节调试";
      },
    );
  text(
    "gripper-value",
    connected && q[offset + 6] != null
      ? "实际 " + (q[offset + 6] * 100).toFixed(1) + "%"
      : "开度 —",
  );
  $("gripper-target").disabled = !(canMaintain && idle && !intervening);
  $("gripper-apply").disabled ||= !!state.jog_active;
  $("gripper-apply").textContent = state.jog_active ? "调节中…" : "应用";
  $("gripper-apply").title = "将所选 Follower 夹爪调至目标开度；不移动臂关节";
  const factoryZero = !!state.factory_zero_home;
  const homeBlockReason = !connected
    ? "机械臂未连接或状态已过期"
    : latched
      ? "请先解除软件急停锁存"
      : !paused
        ? "请先暂停模型或遥操作"
        : recording
          ? "请先结束当前录制"
          : !idle
            ? "请先结束当前维护操作"
            : !state.home_available
              ? state.home_reason || "零位当前不可用"
              : "";
  $("capture-home").hidden = factoryZero;
  $("capture-home").disabled = factoryZero || !(canMaintain && idle);
  $("home").disabled = !!homeBlockReason;
  $("home").title = homeBlockReason;
  $("home-leader").hidden = !factoryZero;
  $("home-leader").disabled = !!homeBlockReason;
  $("home-leader").title = homeBlockReason;
  $("gravity").disabled = !(canMaintain && idle);
  $("gravity-exit").disabled = !(connected && !latched && maint === "gravity");
  text("maintenance-title", factoryZero ? "零位与重力补偿" : "准备位与重力补偿");
  text("home-group-title", factoryZero ? "关节回零" : "准备位");
  text(
    "home-group-copy",
    factoryZero ? "两台 Follower 回到官方关节零位，Leader 不主动运动。" : "保存合适的四臂姿态，供下一次采集恢复。",
  );
  text("home-label", factoryZero ? "Follower 回零" : "回准备位");
  text(
    "home-subtitle",
    factoryZero ? "六关节零位 · Leader 与夹爪保持" : "四臂协调回位 · 夹爪保持当前开度",
  );
  text(
    "home-status",
    latched
      ? "软件急停已锁存 · 现场确认后先解除锁存"
      : !paused
        ? "运行中 · 暂停后可执行设备维护"
        : recording
          ? "录制中 · 结束录制后可执行设备维护"
          : maint === "homing"
      ? factoryZero
        ? `${state.home_group === "leader" ? "Leader" : "Follower"} 正在回零`
        : "正在回准备位，完成后保持不动"
      : maint === "gravity"
        ? "重力补偿中：请手扶机械臂调整姿态"
        : state.home_available
          ? factoryZero
            ? "零位可用 · 回零前请清空完整运动路径"
            : "准备位已保存 · 回位前请清空完整运动路径"
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

  $("init-exit").hidden = !(state.initializing && context.connected);
  $("init-exit").disabled = !(context.canMaintain && context.idle);

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
  const followers = state.initialization?.inventory?.followers || [];
  const gripperRangesPinned =
    followers.length > 0 &&
    followers.every((item) => Array.isArray(item.gripper_limits) && item.gripper_limits.length === 2);
  text(
    "init-arm-status",
    armDone
      ? `四臂反馈正常 · 夹爪行程 ${limits.map((x) => x?.map((v) => Number(v).toFixed(3)).join(" → ") || "—").join(" / ")}`
      : state.connection === "connecting"
        ? "正在顺序连接设备；未固定行程的夹爪会先完成自动标定"
        : gripperRangesPinned
          ? "夹爪范围已保存；连接时不扫行程，四臂保持当前位置"
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
  text("init-cameras", state.camera_cleanup_pending ? "重试断开相机" : "连接相机");
  $("init-arms").disabled =
    !online || !preflight?.ok || !cameraDone || !!state.cleanup_error ||
    !["disconnected", "fault"].includes(state.connection);
  $("init-arms").title = state.cleanup_error
    ? "上次关闭设备失败，请现场检查并重启设备服务"
    : state.connection === "fault" ? "排除故障后可重新初始化；不会自动开始运动" : "";
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
      ? "无需任务即可遥操作；选择采集任务后才开放数据录制。"
      : "查看四臂状态，示教准备位，完成采集前的设备调试。",
  );
}
for (let j = 0; j < 6; j++) {
  const el = document.createElement("div");
  el.className = "joint";
  el.innerHTML = `<div class="joint-head"><span>关节 J${j + 1}</span><strong data-joint-value="${j}">—</strong></div><div class="joint-control-line"><button class="button jog-button" data-joint="${j}" data-delta="-1" aria-label="关节 J${j + 1} 减小">−</button><input class="joint-slider" type="range" min="-180" max="180" step="0.1" value="0" data-joint-slider="${j}" tabindex="-1" aria-label="关节 J${j + 1} 当前角度" /><button class="button jog-button" data-joint="${j}" data-delta="1" aria-label="关节 J${j + 1} 增大">＋</button></div>`;
  $("joint-controls").append(el);
}
document
  .querySelectorAll("[data-page]")
  .forEach((b) => (b.onclick = () => switchPage(b.dataset.page)));
document
  .querySelectorAll("[data-mode]")
  .forEach((b) => (b.onclick = () => {
    if (state.intervention_pending) {
      confirmAction("结束介入并切换模式？", "当前录制将结束，两边保持当前位置；切换后不会自动运动。", () =>
        action("/event/end_intervention:" + b.dataset.mode));
    } else {
      action("/event/mode:" + b.dataset.mode);
    }
  }));
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
$("gripper-target").oninput = () => {
  const input = $("gripper-target");
  text("gripper-percent", input.value !== "" && input.validity.valid
    ? (input.valueAsNumber * 100).toFixed(0) + "%" : "—");
};
$("gripper-apply").onclick = async () => {
  const input = $("gripper-target");
  if (!input.reportValidity()) return;
  $("gripper-apply").disabled = true;
  await action("/jog", { arm, joint: 6, target: input.valueAsNumber });
};
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
    "这将覆盖本机当前配置的准备位。请确认四台机械臂姿态合适；保存不会运动，也不要求 Leader 与 Follower 精确对齐。",
    () => action("/event/capture_home"),
  );
$("home").onclick = () =>
  confirmAction(
    state.factory_zero_home ? "两台 Follower 回零？" : "回到准备位？",
    state.factory_zero_home
      ? "两台 Follower 将沿关节插值路径回到六关节零位；Leader 不接收位置目标，夹爪保持。请确认完整路径没有人员或障碍，可随时按暂停。"
      : "回位将独占四臂控制，并沿关节插值路径运动。请确认完整路径没有人员或障碍；夹爪保持当前开度。可随时按暂停。",
    () => action("/event/home"),
  );
$("home-leader").onclick = () =>
  confirmAction(
    "两台 Leader 回零？",
    "仅两台 Leader 六关节沿插值路径回到零位，Follower 与夹爪保持。请确认长手柄到桌面的完整路径间隙；可随时暂停或急停。完成后 Leader 恢复重力补偿。",
    () => action("/event/home_leader"),
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
function cameraAction() {
  return action(camerasConnected() || state.camera_cleanup_pending
    ? "/cameras/disconnect" : "/cameras/connect");
}
$("init-cameras").onclick = cameraAction;
$("init-arms").onclick = () => {
  const followers = state.initialization?.inventory?.followers || [];
  const gripperRangesPinned =
    followers.length > 0 &&
    followers.every((item) => Array.isArray(item.gripper_limits) && item.gripper_limits.length === 2);
  confirmAction(
    "开始四臂初始化？",
    gripperRangesPinned
      ? "请确认四台机械臂已固定、手柄按钮全部释放且有人照看。当前本站两只 Follower 已保存夹爪范围，连接会施加力矩并保持当前位置，但不会扫夹爪行程、自动回零或启动遥操作。"
      : "请确认两只 Follower 夹爪已手动闭合、夹爪行程无障碍，四台机械臂已固定、手柄按钮全部释放且有人照看。本站尚未保存夹爪范围，连接会执行夹爪标定，但不会自动回零或启动遥操作。",
    () => action("/connect", { ready: true, initialize: true }),
  );
};
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
$("init-exit").onclick = async () => {
  try {
    await post("/initialize/exit");
    await poll();
    showPage("workspace");
    toast("已进入普通工作台；可直接遥操作，或选择任务后切换模式");
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
      state.selected_task
        ? "当前采集任务：" + state.selected_task.name
        : "当前模式：遥操作 · 不录制数据",
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
    if (camerasConnected() && state.preview_enabled !== false) {
      img.hidden = false;
      img.nextElementSibling.hidden = true;
    }
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
      const next = new Image();
      next.onload = () => {
        delete img.dataset.loading;
        if (!camerasConnected() || state.preview_enabled === false) return;
        img.dataset.lastGoodAt = String(Date.now());
        // Keep the old JPEG visible if the next request fails or is still loading.
        img.src = next.src;
      };
      next.onerror = () => { delete img.dataset.loading; };
      next.src =
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
  text("task-badge", task ? "当前任务" : "未选任务");
  text("task-category", "任务 / DATASET TASK");
  text("task-name", task?.name || "无需任务，直接遥操作");
  text(
    "task-instruction",
    task?.instruction || "连接机械臂即可双臂遥操作；需要录制数据时再选择采集任务。",
  );
  text(
    "task-identity",
    task
      ? `任务 ID · ${task.id.slice(0, 8)}`
      : "遥操作不录制，不会混入训练数据",
  );
  text(
    "task-session-tip",
    state.initializing
      ? "当前为初始化会话 · 完成或退出向导后开放任务"
      : locked
      ? task
        ? "暂停并结束介入、维护和录制，保存完成后可切换任务"
        : "先暂停遥操作，保持机械臂连接即可选择任务"
      : state.taskless_teleop
        ? "四臂已保持 · 选择任务即可切换数据采集"
        : "同一任务的每次采集会话独立保存",
  );
  $("create-task").disabled = !online || locked;
  $("create-task").title = state.initializing
    ? "请先完成或退出设备初始化向导"
    : locked
      ? "请先暂停、结束介入与维护，并等待录制保存完成"
      : "";
  $("edit-task").disabled = !online || locked || !task;
  text(
    "task-english",
    task ? "task: " + (task.task || "待补填英文 task，请点击编辑任务") : "不写入训练数据",
  );
  $("choose-task").disabled = !online || locked || !state.tasks?.length;
  $("step-task").classList.toggle("done", !!task);
  $("step-devices").classList.toggle(
    "done",
    activeDevice() && (!task || camerasConnected()),
  );
  $("step-record").classList.toggle("done", !!state.recording);
  text(
    "workflow-tip",
    state.initializing
      ? "设备初始化会话不会写入任务数据 · 完成或退出后进入工作台"
      : !task
      ? state.taskless_teleop
        ? "暂停遥操作后选任务；不需断开机械臂"
        : "可直接连接机械臂遥操作 · 录制前请选择任务"
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
$("connect-cameras").onclick = cameraAction;
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
    text("task-form-error", operatorHint(error.message));
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

function keepPolicyDraft() {
  const fusion = $("policy-fusion").value;
  policyDraft = { fusion };
  if (fusion === "rtc") policyDraft.rtc_delay_steps = Number($("rtc-delay-steps").value);
  $("rtc-delay-steps").disabled = $("policy-fusion").disabled || fusion !== "rtc";
}
$("policy-fusion").onchange = keepPolicyDraft;
$("rtc-delay-steps").onchange = keepPolicyDraft;
$("policy-form").onsubmit = async (e) => {
  e.preventDefault();
  keepPolicyDraft();
  if (policyDraft.fusion === "rtc" &&
      (!Number.isInteger(policyDraft.rtc_delay_steps) || policyDraft.rtc_delay_steps < 1 || policyDraft.rtc_delay_steps > 10)) {
    toast("RTC 前缀步数需为 1–10 的整数");
    return;
  }
  if (await action("/policy/settings", policyDraft)) {
    toast("设置已提交；保持状态下生效，下次模型执行使用新值");
  } else {
    policyDraft = null;
  }
};
$("policy-source-form").onsubmit = async (e) => {
  e.preventDefault();
  const url = $("policy-source-url").value.trim();
  if (await action("/policy/source", { url })) toast("来源切换已提交；机械臂保持连接，不自动运动");
};
$("policy-source-local").onclick = () => { $("policy-source-url").value = "ws://127.0.0.1:8002"; };
$("interaction-reload").onclick = async () => {
  if (await action("/control/reload")) toast("交互规则已提交重载；设备保持连接，不自动运动");
};
$("policy-source-thor").onclick = () => { $("policy-source-url").value = "ws://192.168.250.1:8000"; };
$("policy-restart").onclick = async () => {
  if (await action("/policy/restart")) toast("推理通信子进程正在重载；机械臂保持连接");
};
$("recording-restart").onclick = async () => {
  await action("/recording/restart");
};
$("planner-restart").onclick = async () => {
  if (await action("/policy/planner/restart")) toast("动作规划子进程正在重载；机械臂保持连接");
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
