// js/cloud_comfy_op.js
// 云端运维节点前端（简化版）：
//   - 历史模板 / 删除模板 下拉动态填充
//   - SSH密码 真值存 widget._pwd，显示层强制星号/空
//   - SSH主机 提供 placeholder 提示（优先 DOM，不覆盖 Canvas 绘制）
//   - 运维动作（布尔开关）绑定为一次性触发按钮
//   - 状态写入 + 节点标题栏/状态框着色
// 注意：aki-v3.2 / LiteGraph 用 Canvas 绘制 widget，自定义 canvas 绘制容易导致
//       卡顿与模块缓存不生效；本版只保留 DOM 兜底，Canvas 上保持原生外观。

import { app } from "../../scripts/app.js";

const OP_API = "/custom_script/cloud_comfy_op";
const PROFILES_API = "/custom_script/cloud_comfy_profiles";
const NODE_NAME = "CloudComfyOp";
const PWD_MASK = "********";

function toast(severity, summary, detail) {
  try {
    if (app.extensionManager?.toast?.add) {
      app.extensionManager.toast.add({ severity, summary, detail, life: 8000 });
      return;
    }
  } catch (e) { /* 旧版前端忽略 */ }
  console.log(`[云端运维] ${summary} ${detail ?? ""}`);
}

function getW(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

function findInput(w) {
  if (w.inputEl) return w.inputEl;
  if (w.element) {
    try {
      return w.element.querySelector("input") || w.element.querySelector("textarea");
    } catch (e) { /* 忽略 */ }
  }
  return null;
}

const HOST_PLACEHOLDER = "ssh -p 端口 用户名@主机地址";

function injectPlaceholderCss() {
  if (document.getElementById("cloudop-style")) return;
  const s = document.createElement("style");
  s.id = "cloudop-style";
  s.textContent = `
.cloudop-host-input::placeholder,
input.cloudop-host-input::placeholder {
  color: #8a8a8a !important;
  opacity: 1 !important;
  font-style: italic;
}
`;
  document.head.appendChild(s);
}

function setupHost(node) {
  const w = getW(node, "SSH主机");
  if (!w) return;
  injectPlaceholderCss();
  try { w.placeholder = HOST_PLACEHOLDER; } catch (e) { /* 忽略 */ }
  const inp = findInput(w);
  if (inp) {
    inp.classList?.add("cloudop-host-input");
    inp.placeholder = HOST_PLACEHOLDER;
  }
}

function styleButtons(node) {
  const actions = [
    { name: "连接并检测" },
    { name: "保存模板" },
    { name: "删除" },
    { name: "拉云端日志" },
    { name: "停止云端任务" },
  ];

  actions.forEach(({ name }) => {
    const w = getW(node, name);
    if (!w) return;
    // 仅当 widget 以 DOM 形式渲染时，才用真按钮替换原生 toggle
    if (w.element && !w.element.querySelector(".cloudop-btn")) {
      const el = w.element;
      el.querySelectorAll("input").forEach((c) => { c.style.display = "none"; });
      el.querySelectorAll("span").forEach((s) => {
        const t = (s.textContent || "").trim();
        if (t === "false" || t === "true") s.style.display = "none";
      });
      const btn = document.createElement("button");
      btn.className = "cloudop-btn";
      btn.type = "button";
      btn.textContent = name;
      btn.style.cssText = "width:100%;padding:4px 10px;margin:2px 0;border:none;border-radius:4px;background:#555;color:#fff;font-size:12px;cursor:pointer;";
      btn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (w.callback) w.callback(true);
      };
      el.appendChild(btn);
    }
  });
}

function collectVals(node) {
  const g = (n) => getW(node, n);
  const pw = g("SSH密码");
  const pwd = (typeof pw?._pwd === "string") ? pw._pwd : "";
  return {
    历史模板: g("历史模板")?.value,
    模板名: (g("模板名")?.value || "").toString().trim(),
    SSH主机: (g("SSH主机")?.value || "").toString().trim(),
    SSH密码: pwd,
    删除模板: (g("删除模板")?.value || "").toString().trim(),
  };
}

async function fetchProfiles() {
  try {
    const r = await fetch(PROFILES_API);
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    console.log("[云端运维] 取模板列表失败", e);
    return null;
  }
}

function refreshCombos(node, profiles) {
  const hw = getW(node, "历史模板");
  const dw = getW(node, "删除模板");
  const names = (profiles?.names && profiles.names.length) ? profiles.names : ["(新建)"];
  const last = (profiles?.last && names.includes(profiles.last)) ? profiles.last : names[0];
  if (hw) {
    hw.options = hw.options || {};
    hw.options.values = names;
    hw.value = last;
  }
  if (dw) {
    dw.options = dw.options || {};
    dw.options.values = names;
    let dsel = names[0];
    if (names.length > 1 && names[0] === last) dsel = names[1];
    dw.value = dsel;
  }
}

function setupPassword(node) {
  const w = getW(node, "SSH密码");
  if (!w) return null;

  if (typeof w._pwd !== "string") {
    const cur = w.value ?? "";
    w._pwd = (typeof cur === "string" && cur && cur !== PWD_MASK && !/^[*•.]+$/.test(cur)) ? cur : "";
  }

  const inp = findInput(w);
  let showing = false;

  const capture = () => {
    const cur = w.value ?? "";
    if (typeof cur === "string" && cur && cur !== PWD_MASK && !/^[*•.]+$/.test(cur)) {
      w._pwd = cur;
    }
  };

  const applyDisplay = () => {
    const hasPwd = !!w._pwd;
    w.value = hasPwd ? PWD_MASK : "";
    if (inp) {
      inp.type = showing ? "text" : "password";
      inp.autocomplete = "off";
      inp.value = showing ? (w._pwd || "") : (hasPwd ? PWD_MASK : "");
    }
    node.setDirtyCanvas?.(true, true);
  };

  const setReal = (real) => {
    w._pwd = (real === undefined || real === null) ? "" : String(real);
    showing = false;
    applyDisplay();
  };

  const prevCb = w.callback;
  w.callback = function (v) {
    try {
      if (typeof v === "string" && v && v !== PWD_MASK && !/^[*•.]+$/.test(v)) {
        w._pwd = v;
      }
      if (prevCb) prevCb.apply(this, arguments);
    } finally {
      applyDisplay();
    }
  };

  if (inp) {
    inp.type = "password";
    inp.autocomplete = "off";
    inp.addEventListener("input", () => { w._pwd = inp.value || ""; });
    inp.addEventListener("blur", () => { capture(); showing = false; applyDisplay(); });
    inp.addEventListener("focus", () => { inp.type = "password"; });
  }
  applyDisplay();

  return { setReal, getReal: () => w._pwd || "", capture, applyDisplay };
}

function setStatus(node, msg, ok) {
  const sw = getW(node, "状态");
  if (sw) sw.value = msg;
  node.color = ok ? "#3B6D11" : "#A32D2D";
  if (sw && sw.inputEl) sw.inputEl.style.borderColor = ok ? "#3B6D11" : "#A32D2D";
  node.setDirtyCanvas?.(true, true);
}

// 云端协作进度推送 (后端 cloud_pipe_sender._send_cloud_progress 发来的 cloud_op_progress 事件)
// 把进度文本写进「状态」widget，不画 Canvas 控件。task_id 仅作日志区分，不强制匹配节点。
function setupCloudProgress(node) {
  const sock = app.socket;
  if (!sock || typeof sock.addEventListener !== "function") {
    console.log("[云端运维] socket 不可用, 进度面板监听未注册");
    return;
  }
  const handler = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type !== "cloud_op_progress") return;
      const text = msg.data?.text;
      if (typeof text !== "string") return;
      const sw = getW(node, "状态");
      if (sw) {
        sw.value = text;
        node.setDirtyCanvas?.(true, true);
      }
    } catch (e) { /* 忽略非 JSON / 非本事件消息 */ }
  };
  sock.addEventListener("message", handler);
  // 节点销毁时移除监听, 避免内存泄漏
  const origOnRemoved = node.onRemoved;
  node.onRemoved = function () {
    try { sock.removeEventListener("message", handler); } catch (e) { /* 忽略 */ }
    origOnRemoved?.apply(this, arguments);
  };
}

app.registerExtension({
  name: "自定义脚本.CloudComfyOp",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_NAME) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated?.apply(this, arguments);
      const node = this;

      const pwdHelper = setupPassword(node);
      if (pwdHelper) node._pwdHelper = pwdHelper;

      fetchProfiles().then((p) => { if (p) refreshCombos(node, p); });

      const hw = getW(node, "历史模板");
      if (hw) {
        hw.callback = async (value) => {
          if (!value) return;
          const p = await fetchProfiles();
          const prof = p?.profiles?.[value];
          if (prof) {
            const set = (nm, v) => {
              const x = node.widgets.find((w) => w.name === nm);
              if (x && v !== undefined && v !== null) x.value = v;
            };
            set("SSH主机", prof["SSH主机"]);
            if (node._pwdHelper) node._pwdHelper.setReal(prof["SSH密码"] || "");
            const tw = getW(node, "模板名");
            if (tw) tw.value = value;
          }
          try {
            await fetch(OP_API, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ op: "切换", vals: { 模板名: value } }),
            });
          } catch (e) { /* 忽略 */ }
        };
      }

      const bindSwitch = (name, op, label) => {
        const w = getW(node, name);
        if (!w) return;
        w.callback = async (value) => {
          if (!value) return;
          w.value = false;

          if (op === "删除") {
            const dn = getW(node, "删除模板")?.value || "";
            const cur = getW(node, "历史模板")?.value || "";
            const tip = (dn === cur)
              ? `模板「${dn}」是当前正在使用的，确定删除？`
              : `确定删除模板「${dn}」？`;
            if (!confirm(tip)) return;
          }

          toast("info", label, "执行中…");
          try {
            const body = { op, vals: collectVals(node) };
            const resp = await fetch(OP_API, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
            const json = await resp.json();
            if (!json.ok) {
              toast("error", label, json.message || "失败");
              setStatus(node, json.message || "失败", false);
              return;
            }
            if (op === "连接并检测") {
              const ok = !!json.connected;
              setStatus(node, json.message || "", ok);
              let summary = ok
                ? (json.message && json.message.includes("自动启动") ? "已自动启动并连通" : "已连通")
                : "未连通";
              if (json.data_ok === false) summary += " · 数据往返失败";
              toast(ok ? "success" : "error", summary, json.message || "");
            } else if (op === "启动") {
              const ok = !!json.connected;
              setStatus(node, json.message || "", ok);
              toast(ok ? "success" : "error", ok ? `${label}成功` : `${label}失败`, json.message || "");
            } else if (op === "拉云端日志") {
              setStatus(node, "已拉取云端日志（下方状态框为内容，完整日志见本地文件）", true);
              const sw = getW(node, "状态");
              if (sw) sw.value = json.log || json.message || "";
              node.setDirtyCanvas?.(true, true);
              toast("success", label, json.log_file ? "本地文件: " + json.log_file : (json.message || ""));
            } else {
              setStatus(node, json.message || "", true);
              toast("success", label, json.message || "");
            }
            const p = await fetchProfiles();
            if (p) refreshCombos(node, p);
          } catch (e) {
            toast("error", label, "请求失败: " + e);
            setStatus(node, "请求失败: " + e, false);
          }
        };
      };

      bindSwitch("连接并检测", "连接并检测", "连接并检测");
      bindSwitch("保存模板", "保存模板", "保存模板");
      bindSwitch("删除", "删除", "删除");
      bindSwitch("拉云端日志", "拉云端日志", "拉云端日志");
      bindSwitch("停止云端任务", "停止云端任务", "停止云端任务");

      setupHost(node);
      styleButtons(node);
      setupCloudProgress(node);

      const origConfigure = node.onConfigure;
      node.onConfigure = function () {
        origConfigure?.apply(node, arguments);
        if (node._pwdHelper) {
          node._pwdHelper.capture();
          node._pwdHelper.applyDisplay();
        }
        fetchProfiles().then((p) => {
          if (!p) return;
          refreshCombos(node, p);
          if (node._pwdHelper && node._pwdHelper.getReal() === "") {
            const prof = p.profiles?.[p.last];
            if (prof) node._pwdHelper.setReal(prof["SSH密码"] || "");
          }
        });
      };

      return r;
    };
  },
});
