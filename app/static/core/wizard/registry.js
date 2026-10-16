/* WizardRegistry: 通用挂载入口 (v0.4 插件化 — 零耦合硬要求)
 *
 * 唯一对外暴露: window.WizardRegistry.{tryMount, unmount}
 *   - tryMount(manifest) — 读 manifest.wizard_config,缺失 → 静默 return;存在 → 加载 wizard.json 并挂载
 *   - unmount()          — 销毁当前 wizard DOM,触发应用 init
 *
 * 不维护任何应用清单,不 import 任何 app.apps.* 模块;只通过 manifest 字段声明式交互。
 */
(function () {
  "use strict";

  const LOG_PREFIX = "[wizard-registry]";
  let _loadingConfig = null; // promise cache: 同 manifest 多次 tryMount 只加载一次 wizard.json

  /**
   * 加载 wizard.json;失败吞掉回 null(上层 tryMount 据此决定 skip)。
   * path 是 manifest 里的 wizard_config 字符串,如 "wizard.json" / "/static/apps/video-detection/wizard.json"。
   */
  async function _loadConfig(path) {
    // 绝对路径 / 相对路径都支持
    const url = path.startsWith("/") ? path : `/static/apps/${_currentAppId}/${path}`;
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) {
      console.warn(LOG_PREFIX, "fetch", url, "→ HTTP", r.status);
      return null;
    }
    return r.json();
  }

  let _currentAppId = null; // 由 tryMount 写入;保证 wizard.json 相对路径解析

  async function tryMount(manifest, opts) {
    opts = opts || {};
    try {
      if (!manifest || !manifest.wizard_config) return;       // 零开销跳过:无 wizard_config
      _currentAppId = manifest.id;
      _loadingConfig = _loadConfig(manifest.wizard_config);
      const cfg = await _loadingConfig;
      _loadingConfig = null;
      if (!cfg) return;
      if (cfg.enabled === false) return;
      if (opts.force) {
        // 显式重开路径("向导模式" 按钮触发):无论之前是否完成都强制挂,
        // 并清掉"已完成"标记 + 上次停留的步骤,让用户从第 1 步重配。
        // force 不绕过 cfg.enabled / mount 缺失这种结构性检查 —— 那些
        // 跳过条件仍然生效(没人能"强制"挂一个 disabled 的向导)。
        try { window.WizardState?.clearCompleted?.(manifest, cfg); } catch (_) {}
        try { window.WizardState?.clearStep?.(manifest, cfg); } catch (_) {}
      } else if (window.WizardState?.isCompleted?.(manifest, cfg)) {
        // 自动挂载路径(framework 在 showPage 里调):用户之前已经走完
        // _finish (applyWizardSnapshot 成功落到了下游),再次进入不应
        // 自动弹向导,避免"已完成的应用每次切回都重弹第 1 步"的既视
        // 感 —— 这正是 v0.22 之前那个老 bug。
        return;
      }
      if (!window.WizardState || !window.WizardState.mount) {
        console.warn(LOG_PREFIX, "WizardState not loaded — skip mount");
        return;
      }
      await window.WizardState.mount(manifest, cfg);
    } catch (e) {
      console.warn(LOG_PREFIX, "tryMount failed:", e && (e.message || e));
    } finally {
      _loadingConfig = null;
    }
  }

  function unmount() {
    try {
      if (window.WizardState && window.WizardState.unmount) {
        window.WizardState.unmount();
      }
    } catch (e) {
      console.warn(LOG_PREFIX, "unmount failed:", e && (e.message || e));
    }
  }

  window.WizardRegistry = { tryMount, unmount };
})();