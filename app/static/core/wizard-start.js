/* wizard-start —— 向导「完成配置」之后的通用启动器。
 *
 * 为什么需要它（2026-10-23 实测到的回归）：
 *
 *   卡片原本的向导启动方式是 `btn.click()`（模拟用户点「启动检测」）。自
 *   6ea16e9「模型能力未启用时置灰启动按钮」起，启动按钮的 `disabled` 由
 *   卡片的 **1Hz 快照**（`snapshot.session_ready`）决定 —— 快照比 registry
 *   慢一拍。模型刚起来的那一秒（向导 env 步刚把它拉起来 → finish 立刻
 *   点启动），按钮还是灰的，而 **disabled 按钮的 `click()` 被浏览器静默
 *   丢弃**：不报错、不打日志、不触发 handler。于是：
 *
 *     - 向导显示「完成配置」✓
 *     - 功能页上却什么都没跑 ✗
 *
 *   实测（单机板，journal 证据）：模型 `→ running` 与向导 finish 的 open
 *   落在同一秒（08:19:35 / 08:24:34~35），卡片的 WS 快照要到下一秒
 *   （08:19:36 / 08:26:26）才送来 `session_ready=true` —— 按钮在这中间
 *   是灰的，`click()` 被吃掉，「音视频描述」「视频任意目标定位」两个卡片
 *   向导跑完不启动；而「视频目标检测」能跑，只是因为它的模型提前 ≥1s
 *   就绪（纯看运气）。
 *
 * 本模块把「等就绪 → 真启动 → 失败可见」三件事收成一处：
 *
 *   1. **等能力真的就绪** —— 直接问后端快照（`probe`），不等 WS 那一拍；
 *   2. **直接调卡片自己的启动函数** —— 绕开按钮的 `disabled`；
 *   3. **等不到 / 没启动成功 → 抛错** —— 向导会显示错误（可重试的还会给
 *      「重新开启模型」），不再"静默成功"。
 *
 * 框架不认识任何卡片：就绪判据、启动动作都由调用方给。
 */
(function () {
  "use strict";

  // 等多久算「模型还没起来」。向导框架自己已经等过模型 running（registry
  // 口径），这里等的是**卡片**的口径（session_ready），两者差的就是那一拍
  // 快照延迟（≤1s）—— 20s 足够覆盖它，同时也够覆盖"registry 说 running
  // 但会话还没建好"的短暂窗口。再久就是模型真没起来，该报错而不是干等。
  const DEFAULT_TIMEOUT_MS = 20000;
  const DEFAULT_POLL_MS = 400;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function _err(code, detail, retryable) {
    const e = new Error("WizardSnapshotError:" + code + ":" + detail);
    // retryable=true → 向导的 finish 错误框会多给一个「重新开启模型」按钮
    e.retryable = !!retryable;
    return e;
  }

  /**
   * 等就绪 → 启动 → 校验结果。
   *
   * @param {object} o
   * @param {() => Promise<object|null>} o.probe
   *        刷新一次后端快照并返回它（卡片自己的 refresh*Detector）。
   * @param {() => Promise<any>} o.start
   *        卡片自己的启动函数（**不要**用 btn.click() —— 见文件头）。
   * @param {string} [o.label]        人类可读的名字，错误信息里用。
   * @param {(snap) => boolean} [o.isReady]   快照是否代表"能力就绪"。
   *        默认 `!!snap.session_ready`（三张视频卡的 detector 快照同口径）。
   * @param {(snap) => boolean} [o.isStarted] 启动后快照是否代表"已经在跑"。
   *        默认 `!!snap.enabled`。
   * @param {number} [o.timeoutMs]
   * @param {number} [o.pollMs]
   * @returns {Promise<object>} 启动后的快照
   */
  async function run(o) {
    const opts = o || {};
    const probe = opts.probe;
    const start = opts.start;
    const label = opts.label || "启动功能";
    const timeoutMs = Number.isFinite(opts.timeoutMs) ? opts.timeoutMs : DEFAULT_TIMEOUT_MS;
    const pollMs = Number.isFinite(opts.pollMs) ? opts.pollMs : DEFAULT_POLL_MS;
    const isReady = opts.isReady || ((s) => !!(s && s.session_ready));
    const isStarted = opts.isStarted || ((s) => !!(s && s.enabled));

    if (typeof probe !== "function" || typeof start !== "function") {
      throw _err("bad_usage", label + " —— 缺少 probe/start 回调", false);
    }

    // 1. 等就绪（直接问后端，不等 WS 那一拍）
    const deadline = Date.now() + timeoutMs;
    let snap = null;
    let lastErr = null;
    while (Date.now() < deadline) {
      try {
        snap = await probe();
        lastErr = null;
      } catch (e) {
        snap = null;
        lastErr = e;
      }
      if (isReady(snap)) break;
      await sleep(pollMs);
    }
    if (!isReady(snap)) {
      const why = lastErr ? ("(" + (lastErr.message || lastErr) + ")") : "";
      console.warn("[wizard-start]", label, "等就绪超时", why);
      throw _err("capability_not_ready",
        label + " —— 模型还没就绪(或已被关闭)" + why + "，请开启模型后重试。", true);
    }

    // 2. 真启动 —— 直接调卡片自己的函数，绕开按钮的 disabled
    await start();

    // 3. 校验：卡片的启动函数失败时只往 overlay 写一句话、不抛异常，
    //    这里再问一次后端 —— 没跑起来就必须让向导报错，不能"静默成功"。
    let after = snap;
    try {
      const s2 = await probe();
      if (s2) after = s2;
    } catch (_) { /* 保持 start() 之前的快照 */ }
    if (!isStarted(after)) {
      console.warn("[wizard-start]", label, "启动后状态仍是", after && (after.state || after.enabled));
      throw _err("start_failed",
        label + " 没能启动 —— 请到功能页看提示(提示词为空 / 没勾模型实例 / 模型未就绪)。", false);
    }
    return after;
  }

  window.WizardStart = { run };
})();
