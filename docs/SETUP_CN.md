# ComfyUI-CloudPipe 节点详解与安装（中文）

本文补充说明每个节点的外观/参数，以及云端必须的 **Easy-Use `easy fullkSampler`** 依赖。

---

## 一、节点一览

### ☁️ 云端运维（CloudComfyOp）—— 本机用

```
┌─────────────────────────────────────────┐
│ ☁️ 云端运维                                │
├─────────────────────────────────────────┤
│ 历史模板      [下拉: (新建) ▼]            │
│ 模板名        [__________]                │
│ SSH主机       [ssh -p 端口 用户@主机]      │  ← 灰字提示
│ SSH密码       [********]                  │  ← 星号显示，真值在内存
│ 连接并检测    [ 按钮 ]                    │
│ 重启          [ 按钮 ]                    │
│ 保存模板      [ 按钮 ]                    │
│ 删除模板      [下拉 ▼]  删除 [按钮]       │
│ 状态          [多行文本框，显示日志]      │
└─────────────────────────────────────────┘
```

- **SSH主机**：填 `ssh -p 端口 用户名@主机地址`，例如 `ssh -p 12345 root@connect.cqa1.seetacloud.com`（端口用你自己实例的）
- **SSH密码**：只存本机加密文件，绝不外传
- **连接并检测**：连通后节点变绿，自动探测 GPU/CPU 并做数据往返测试
- **重启**：重启云端 ComfyUI 服务（用于改配置/清显存）
- **保存模板 / 删除模板**：把连接存成本机加密模板，下次下拉选
- **状态**：点连接后实时显示日志（无需跑队列）

> ⚠️ 这个节点是**独立**的，不要接进主工作流链路。它靠按钮触发，接进队列执行会打断流程（已做静默处理，不会报错，但没必要接）。

---

### 🔄 A·云端协作（CloudPipeAsync）—— 本机用

```
输入 : 管道 (PIPE_LINE)   ← 接 AnimaPipePack 等打包工具
参数 : 步骤 / CFG / 采样器 / 调度器 / 降噪 / 种子 / 图像输出
输出 : 管道 (PIPE_LINE)   → 接 VAEDecode
```

点队列执行时自动：SFTP 上传 cond/latent → 提交云端工作流 → 轮询 → 下载结果 → 输出新管道。

---

### 📥 B·云端接收（CloudLoadInputs）—— 云端用

```
输入 : model (MODEL) + clip (CLIP) + 管道 (STRING, task_id)
输出 : 管道 (PIPE_LINE)
```

从云端 `input/` 读 A 上传的 `pos_cond_*.pt` / `neg_cond_*.pt` / `cloud_init_*.pt`，用云端 model/clip 组装管道。**「管道」是 STRING 触发器，由 A 自动填入，你不用手填。**

---

### 📤 C·云端结果传回（CloudSendBack）—— 云端用

```
输入 : 管道 (PIPE_LINE) + task_id (STRING)
输出 : 管道 (PIPE_LINE)  ← pass-through
```

把采样后的管道序列化到云端 `output/cloud_result_<task_id>.pipe.pt`，供 A 下载。

---

### 🔁 云端测试回传（CloudTestEcho）—— 云端用

测试节点，不加载模型，原样回传本地上传的数据，验证「本地↔云端」传输无损。**普通用户不用手动放，运维节点的「连接并检测」会间接用到。**

---

## 二、Easy-Use 的 `easy fullkSampler`（云端必须）

### 它是什么

`easy fullkSampler` 是 **ComfyUI-Easy-Use** 自定义节点包里的一个采样器节点。它和官方 `KSampler` 不同：
- 输入是 **PIPE_LINE**（管道），不是零散的 model/positive/negative
- 自带完整的采样参数（steps/cfg/sampler_name/scheduler/seed 等）
- A 节点自动拼的云端工作流就是用它做采样

**没有 Easy-Use，云端工作流跑不起来。**

### 安装（云端实例上）

在云端 ComfyUI 的 `custom_nodes/` 目录执行：

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/yolain/ComfyUI-Easy-Use.git
cd ComfyUI-Easy-Use
pip install -r requirements.txt
```

重启云端 ComfyUI。在节点菜单搜 `easy fullkSampler` 能看到即安装成功。

> 群友反馈找不到 `easy fullkSampler`：八成是云端没装 Easy-Use，或装了但没重启。

### 云端工作流怎么搭

导入仓库里的 `examples/cloud_workflow.json`（云端 ComfyUI 里 `Load` → `Open` 选这个文件），结构就是：

```
[UNETLoader] ─┐
[CLIPLoader] ─┴─→ [📥 B·云端接收] → [easy fullkSampler] → [📤 C·云端结果传回]
```

注意：
1. `UNETLoader` / `CLIPLoader` 的模型名要改成你云端真实路径（与 A 节点 `CLOUD_UNET/CLOUD_CLIP` 对应）
2. 如果你的模型要加 LoRA，在 B 和采样器之间插 Easy-Use 的 `easy loraStack` / `CR Apply LoRA Stack`（A 节点默认已拼了 loraStack，云端放好 `CLOUD_LORA` 文件即可）
3. 这个工作流在云端**常驻运行**（开着网页别关），A 节点会自动提交任务给它

---

### H3 方案云端依赖（新增，搭配 A/B/C H3 三件套）

H3 视频云端协作（B 方案）在云端侧需要额外模型/节点，与 Anima 方案的 `easy fullkSampler` 互不冲突。请在云端实例准备：

| 依赖 | 说明 | 大小/备注 |
|------|------|-----------|
| ref2va UNET | H3 参考图转视频的主模型（云端 `UNETLoader` 加载） | 约 20GB |
| `minimax_h3_fl2v_turbo_8step` LoRA | turbo 8 步加速 LoRA，接 `LoraLoaderModelOnly` | 见发布页 |
| `MinimaxH3LatentUpscalerNode3D` 节点 + bf16 模型 | latent（潜空间张量）上采样器节点及其权重 | 约 691MB（bf16，bfloat16 16 位浮点） |
| H3 视频/音频 VAE | 视频/音频的变分自编码器（编解码潜空间） | 随 H3 节点包 |

> 注：CLIP（qwen3vl，文本编码器）**本机端需要**（A 节点随 `positive` 一起上传）；**云端 B 端不需要**，因此云端省去约 27GB 的 CLIP 加载。详见 README「H3 视频云端协作（B 方案）」。

---

## 三、节点示意图

见 [nodes.svg](nodes.svg) —— 本机/云端节点拓扑与连接关系一目了然。

---

## 四、常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 云端找不到 `easy fullkSampler` | 云端没装 Easy-Use | 装 ComfyUI-Easy-Use 并重启 |
| A 报错「缺少条件文件」 | B 没读到 A 上传的 .pt | 确认云端也装了本节点包，且 B 工作流在跑 |
| 连接变红「SSH 不通」 | 云端实例关机/密码错 | 去控制台开机，或在运维节点重填密码 |
| 状态显示「CPU 模式」 | 云端实例是 CPU | 控制台切 GPU 模式 |
| 前端按钮没变 | 浏览器缓存 | 关标签页重开或 Ctrl+F5 |
