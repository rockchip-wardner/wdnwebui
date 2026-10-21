# Stage 3 人工回归清单（7 张卡片）

Stage 3 把 worker 的推理链路整体换成了
`wdndds.serve_local_session` / `LLMRPCSession`，线协议、schema 解析位置、
流式通道全部变了。自动化 smoke 覆盖了 RPC 层，下面这份是**只能在浏览器
里观察**的部分：向导、WS 推送、前端渲染、历史记录、工具徽章。

跑之前：

```bash
cd /home/rockchip/wdnwebui
pkill -9 -f compute_worker            # 清掉打补丁前代码的孤儿 worker
.venv/bin/python run.py               # uvicorn 会自己 spawn 新 worker
```

打开 `http://<板子IP>:8000`。

> **每张卡片首次进入都会弹向导**（v0.4 的设计，不是回归）。走完向导再测
> 主流程。wizard 完成后会自动调 `applyWizardSnapshot` 并触发启动按钮。

---

## 1. 应用面板 `#apps`

- [ ] 8 张卡片全部渲染，无破图、无 `fatal` 标记
- [ ] 已加载模型的卡片显示 `running`，未加载的显示 `unavailable`
- [ ] 卡片预览视频正常播放

## 2. 算力配置 `#compute-config`

- [ ] 7 个 worker 全部列出，名称/显存/预估时间与 `manifest.json` 一致
- [ ] **启用** `qwen3.5-vl-4b` → 状态 `loading` → `running`（约 50s）
- [ ] **禁用** → 进程退出，卡片回落 `not_loaded`
- [ ] 启用→禁用→再启用，反复 3 次，**无重复进程**（看系统信息页的进程数）
- [ ] 已启用时禁用 `qwen3-vl-4b`（互斥替代品）→ 应报 403 并提示"到目标板子上禁用"

## 3. 监控反查 `video-retrieval` / `#video-retrieval`

- [ ] 向导 → 选模型 → 启动
- [ ] **视频分段**提取检索索引
- [ ] 提取成功后，提取结果会被存入缓存
- [ ] 提取步骤结束，会开始根据提问进行反向检索
- [ ] 切走页面时 DemoGuard 弹确认框；确认后后台确实停

## 4. 视频目标检测 `video-target-detection`

- [ ] 启动后能出事件（**注意**：Qwen3.5-VL 经常把 schema 原样吐回来，
      这时事件文本是 `[TargetDoc 解析失败]` —— 这是**已知的模型缺陷**，
      不是本次回归。旧版本同样如此。判断标准：有没有 bbox 事件出现，
      以及没有 bbox 时文本是"解析失败"占位而不是空白）
- [ ] 出现 bbox 事件时，裁图和 overlay 对得上目标
- [ ] bbox 是 0–1000 归一化坐标（ROI 不贴在左上角 0 像素处）
- [ ] 768 档位（`#vd-detect-size` 切 768）下 bbox 仍正确

## 5. 视频目标定位 `video-target-localization`

- [ ] 事件里出现 `<box>` / `<ref>` 标记且被正确渲染成框
- [ ] 整帧 base64 JPEG 能加载出来（弹框里能看到原图）
- [ ] 多目标时一个事件带多个框

## 6. 文本对话 `text-chat`

- [ ] 流式逐字渲染，thinking / answer 分频道
- [ ] **多轮**：连问 3 轮，历史被正确带入，第 3 轮仍记得第 1 轮
- [ ] **超长上下文**：粘一段 >16384 tokens 的文本 → 应报"单条消息过长"，
      而不是把模型撑爆或静默截断
- [ ] **工具徽章**：问一个会触发 `TtsAlert` 的问题（如"朗读一句测试"）
      → 气泡上出现 `🔧 已调用 TtsAlert`，且真的出声
- [ ] **停止**：生成中途点停止 → 立即显示"已停止"，本轮不写入历史，
      下一轮能正常开始
- [ ] **重置**：点重置 → history 清空

## 7. 语音生成 `tts`

- [ ] 出一段 WAV 能播
- [ ] 换 `ref_speaker` / 调整音量后音色/音量有变化

## 8. 注册发现 `#discovery` + 系统信息 `#system-monitor`

- [ ] worker 进程出现在进程列表里，comm 名正确（如 `worker:q5-vl`，
      **13 字符以内**，`/proc/<pid>/comm` 的硬限制）
- [ ] 内存占用没有异常增长（反复启停 5 次后 RSS 应基本平稳）

---

## 已知的、非回归的现象

| 现象 | 原因 |
|---|---|
| 视频目标检测事件是 `[TargetDoc 解析失败]` | Qwen3.5-VL 把 SDK 注入的 schema hint 原样吐回。旧版本同样如此（worker 侧只打日志 + 置 marker，兜底一直是主进程的 `_try_rescue_target_doc`）。自动化 `rescue/*` 四种形态全部通过。 |
| 流式内容偶尔整块出现而不是逐字 | delta 走 RELIABLE topic，中间样本可能被合并。**唯一保证是「拼接 == 返回值」**，不要断言逐块内容。 |
| 点停止后要等一会儿才真的停 | rknn3 没有协作式取消。停止是软停：标志发到旁路 topic，worker 跑完当前这一轮才生效。 |
| 微信桥接回复里偶尔看到 `user` / `<｜im_end｜>` | 模型越界生成。`sanitize_chat_output` 会截断，若仍出现说明模型没听话，是模型行为不是传输问题。 |

## 出问题怎么定位

| 症状 | 先看哪 |
|---|---|
| 整个 worker 没反应（所有 RPC 超时） | worker 日志里有没有 `session not loaded` / 泵相关 WARNING。**注意同名 participant**：每个 worker 只能有一个 `wdnai-serve-*`，两个 service 必须共用 side hub |
| 单个能力卡住，其它正常 | 该 worker 的 `infer_timeout_s`（60s）到点了吗 → 说明 NPU 卡死，需要重启 worker 而不是重试 |
| 模型答非所问 / 缺工具 | `chat_agent._tool_classes` 查到类了吗；prompt 里有没有工具 schema |
| 事件文本是半截 JSON | reply 的 shm 侧文件读失败 → 看 worker 日志的 `reply shm` / `side-file vanished` |

复现命令：

```bash
# 全能力真机 smoke（会自己 spawn / 停止 worker，约 6-8 分钟）
WDNAI_MODELS_DIR=/mnt/oem/models .venv/bin/python scripts/verify_stage3_smoke.py --all

# 单组
WDNAI_MODELS_DIR=/mnt/oem/models .venv/bin/python scripts/verify_stage3_smoke.py --group chat
```