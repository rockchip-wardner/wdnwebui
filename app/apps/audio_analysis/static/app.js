(function () {
  "use strict";
  // 音轨分析面板在「音视频描述」卡（video_detection）里，那里的 app.js 挂
  // 共享模块 app/static/core/audio-analysis.js。
  //
  // 本卡片是 hidden 的，模板里没有音轨面板，**不能**自己再 mount 一次：
  // mount 会开自己的 WebSocket、自己维护一份事件列表，而
  // document.getElementById("vd-audio-events") 拿到的是 vd 卡那个同名元素
  // → 两个渲染器抢同一个 <ol>，表现为「事件框里两套结果反复横跳」，且只有
  // 按钮所属的实例会被 onStart 清空，另一个留着上一轮的旧事件。
  //
  // 共享模块本身有「同一套 id 只挂一次」的幂等保护，这里不重复挂。
  // 本卡片只负责后端 API（manifest 的 register_api）。
})();
