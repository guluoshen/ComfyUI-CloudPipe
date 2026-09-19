// js/cloud_pipe_test.js
// A 节点「🔍 测试」开关：勾选即测（前端直连接口，不走 ComfyUI 图执行）。
//
// 实现要点（2026-08-21 晚安全重写，替代第一版 addDOMWidget 方案）：
//   - 不用 DOM 按钮/不引入自定义 widget：直接监听节点上「测试」BOOLEAN widget 的 callback；
//   - 勾选瞬间：先重置 widget 为 false（防止值进入图执行/污染序列化）→ 立即 fetch 后端
//     /custom_script/cloud_pipe_test → Toast 显示 PASS/FAIL；
//   - 后端 FUNCTION 里 测试 参数恒为 False；万一竞态以 True 进入，后端直接 raise 提示，
//     不会把 4D latent 管道传下游（根治 VAEDecode tuple index out of range）。
//
// 部署：由 __init__.py 启动时复制到 comfyui_frontend_package/static/extensions/cloudtest/
// 下才生效；import 路径层级对应 static/extensions/cloudtest/（三级 ../../scripts/app.js）。

import { app } from "../../scripts/app.js";

const TEST_API = "/custom_script/cloud_pipe_test";
const NODE_NAMES = new Set(["CloudComfyManager"]);
const DEFAULT_URL = "http://127.0.0.1:6006";

function toast(severity, summary, detail) {
  try {
    if (app.extensionManager?.toast?.add) {
      app.extensionManager.toast.add({ severity, summary, detail, life: 8000 });
      return;
    }
  } catch (e) { /* 旧版前端无 toast API 时忽略 */ }
  console.log(`[Cloud测试] ${summary} ${detail ?? ""}`);
}

app.registerExtension({
  name: "自定义脚本.CloudPipeTest",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!NODE_NAMES.has(nodeData.name)) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated?.apply(this, arguments);

      // 找「测试连接」开关
      const testW = this.widgets?.find((x) => x.name === "测试连接");
      if (testW) {
        testW.label = "🔍 测试连接";
        testW.callback = async (value) => {
          if (!value) return;
          // 立即重置，防止进入图执行 / 污染序列化
          testW.value = false;

          // 读「云端HTTP地址」widget 当前值
          const urlW = this.widgets?.find((x) => x.name === "云端HTTP地址");
          const url =
            urlW && typeof urlW.value === "string" && urlW.value.trim()
              ? urlW.value.trim()
              : DEFAULT_URL;

          toast("info", "云端连通测试", "测试中… (约几秒)");
          try {
            const resp = await fetch(TEST_API, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ 云端HTTP地址: url }),
            });
            const json = await resp.json();
            toast(
              json.ok ? "success" : "error",
              "云端连通测试",
              json.message || "无返回"
            );
          } catch (e) {
            toast("error", "云端连通测试", "请求失败: " + e);
          }
        };
      }

      return r;
    };
  },
});
