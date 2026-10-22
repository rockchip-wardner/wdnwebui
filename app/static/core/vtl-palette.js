/* vtl-palette —— VTL/VTD 共享的目标框颜色调色板(20 色循环)。
 *
 * v0.4 之前 const 写在 VTD 的 app.js IIFE 里,同名的 VTL 在
 * ``wdnapps_video_analyzer.cards.video_target_localization 的 app.js`` 引用 ``vtlPaletteRGB``,
 * 但因为 IIFE 作用域封闭,VTL 跑起来裸名 undefined → pageerror。
 *
 * 抽到 core 后两边都用 ``const vtlPaletteRGB = window.vtlPaletteRGB;
 * const vtlLabelColor = window.vtlLabelColor;`` 拿到别名即可。
 */
(function () {
  "use strict";

  const vtlPaletteRGB = [
    [255, 56, 56],   [255, 157, 151], [255, 112, 31],  [255, 178, 29],
    [207, 210, 49],  [72, 249, 10],   [146, 204, 23],  [61, 219, 134],
    [26, 147, 52],   [0, 212, 187],   [44, 153, 168],  [0, 194, 255],
    [52, 69, 147],   [100, 115, 255], [78, 55, 230],   [255, 30, 175],
    [182, 33, 158],  [255, 172, 199], [196, 187, 145], [156, 156, 156],
  ];

  const vtlLabelColorMap = new Map();
  function vtlLabelColor(label) {
    const key = (label && typeof label === "string") ? label : "__empty__";
    if (!vtlLabelColorMap.has(key)) {
      vtlLabelColorMap.set(key, vtlLabelColorMap.size);
    }
    return vtlPaletteRGB[
      vtlLabelColorMap.get(key) % vtlPaletteRGB.length
    ];
  }

  window.vtlPaletteRGB = vtlPaletteRGB;
  window.vtlLabelColor = vtlLabelColor;
})();