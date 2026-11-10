#!/usr/bin/env python3
"""回归测试: 语音生成卡的音色 / 语种清单与「参考音色 vs 内置音色」分流。

背景: Qwen3-TTS 有两条互不相干的发声路径 ——
``ref_speaker``(参考音色 / 声音克隆,SDK 内嵌 6 个参考音色)
和 ``speaker``(模型自带的内置音色,SDK ``SPK_ID`` 的 9 个,向量在模型目录
的 ``talker_input_embed.fp16.bin`` 里)。原来 UI 只露了前一条。

两个坑:
1. ``POST /api/tts/start`` 原来把 ``ref_speaker`` 默认填成 news_female,
   而 SDK 里 ``ref_speaker`` 优先级**高于** ``speaker`` —— 就算前端正确
   传了 ``speaker``,发出来的仍是 news_female。修法: 两个都不给才回落。
2. 内置音色只在 **CustomVoice** 权重里训练过;Base 权重里那几行是未训练
   的噪声(norm 0.022~0.023,训练过的行 0.056~1.64)—— 塞给 talker 等于
   没给音色条件,模型每次生成都自由发挥。所以清单只列 SDK
   ``available_config_speakers()`` 实测可用的,探测不过的一个都不露。

四段,全部不占 NPU:
A. API 层的 speaker / ref_speaker 分流(fake 掉 session,只看收到的
   TTSRequest)—— 真回归点;
B. ``GET /api/tts/voices`` 清单 + 独立复核探测器(直接读模型文件);
C. 前端静态检查;
D. 浏览器实测(下拉真渲染、提示联动、payload 分流)。
"""
import asyncio
import json
import re
import sys
from pathlib import Path

# 让 ``active_app_guard`` 可导入（拦下改生产配置的 active-app 写操作）
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from active_app_guard import (  # noqa: E402
    install_on_page as install_active_app_guard)

API_BASE = "http://127.0.0.1:8000"

# 官方 Qwen3-TTS 的 10 种语言 + auto
EXPECT_LANGUAGES = {
    "auto", "chinese", "english", "japanese", "korean", "german",
    "french", "russian", "portuguese", "spanish", "italian",
}
# 官方 CustomVoice 的 9 个内置音色 —— **只是上限**,本模型未必都带。
# 部署的是声音克隆版(Base)权重时那 9 行是没训练的占位噪声,列出来
# 用户选了也不生效(模型每次自由发挥:时而男声时而女声)。真正该断言的
# 是「清单里的每个音色都通过了 SDK 的有效性探测」,见 B2 / B7。
EXPECT_BUILTIN_MAX = {
    "serena", "vivian", "uncle_fu", "ryan", "aiden",
    "ono_anna", "sohee", "eric", "dylan",
}
# 上游 demo 头文件里只有这两个参考音色
EXPECT_REF = {
    "news_female", "baby_girl", "lovely_girl",
    "cool_lady", "male_announcer", "uncle_bass",
}
# 带方言的内置音色
EXPECT_DIALECT = {"eric": "sichuan_dialect", "dylan": "beijing_dialect"}


def _sdk_builtin(model_dir=None) -> set:
    """SDK 探测到的、本模型真正可用的内置音色。"""
    from wdnai.backends.tts import available_config_speakers
    if model_dir is None:
        from wdnapps_audio_chat.cards.tts.service import get_session
        model_dir = get_session().model_dir
    return set(available_config_speakers(model_dir))


def _api_voices() -> list:
    """后端清单里的全部音色(含用户音色)。"""
    import urllib.request
    with urllib.request.urlopen(f"{API_BASE}/api/tts/voices", timeout=10) as r:
        return json.loads(r.read().decode()).get("voices") or []

ok_count = 0
failed: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_count
    if cond:
        ok_count += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        failed.append(name)
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


# --------------------------------------------------------------------------- #
# A. API 层的 speaker / ref_speaker 分流
# --------------------------------------------------------------------------- #
async def test_api_mapping() -> None:
    print("\n=== A. API 层: speaker / ref_speaker 二选一 ===")
    from wdnapps_audio_chat.cards.tts import api as tts_api

    captured = {}

    class _FakeSession:
        model_dir = None

        def start(self, req):
            captured["req"] = req
            return {"state": "loading"}

    tts_api.get_session = lambda: _FakeSession()
    # 分流要和「音色可用性校验」分开测:这里把清单 patch 成一个假的
    # (内置音色存在),只看 speaker / ref_speaker 有没有**互斥**;
    # 真的可用性由 B8 拿真端点验。
    tts_api.voice_catalog = lambda model_dir=None: {
        "voices": [
            {"id": "serena", "kind": "builtin", "dialect": ""},
            {"id": "news_female", "kind": "ref", "dialect": ""},
            {"id": "baby_girl", "kind": "ref", "dialect": ""},
            {"id": "lovely_girl", "kind": "ref", "dialect": ""},
            {"id": "cool_lady", "kind": "ref", "dialect": ""},
            {"id": "male_announcer", "kind": "ref", "dialect": ""},
            {"id": "uncle_bass", "kind": "ref", "dialect": ""},
        ],
        "languages": [{"id": "auto"}],
    }

    async def call(payload):
        captured.clear()
        await tts_api.tts_start(payload)
        return captured["req"]

    # A1: 只给内置音色 → ref_speaker 必须为空(news_female 会把路堵死)
    req = await call({"text": "你好", "speaker": "serena"})
    check("A1 只传 speaker → speaker 保留",
          req.speaker == "serena", f"speaker={req.speaker!r}")
    check("A1 只传 speaker → ref_speaker 为空",
          req.ref_speaker == "", f"ref_speaker={req.ref_speaker!r}")

    # A2: 只给参考音色 → speaker 必须为空
    req = await call({"text": "你好", "ref_speaker": "baby_girl"})
    check("A2 只传 ref_speaker → ref_speaker 保留",
          req.ref_speaker == "baby_girl", f"ref_speaker={req.ref_speaker!r}")
    check("A2 只传 ref_speaker → speaker 为空",
          req.speaker == "", f"speaker={req.speaker!r}")

    # A3: 两个都不给 → 回落默认参考音色(历史行为不变)
    req = await call({"text": "你好"})
    check("A3 都不传 → 回落 news_female",
          req.ref_speaker == "news_female" and req.speaker == "",
          f"ref_speaker={req.ref_speaker!r} speaker={req.speaker!r}")

    # A4: 语言 / instruct 透传
    # instruct 只作 SDK 透传,**页面不暴露**(Base 权重没有 instruction
    # control,官方能力表该列为空)—— API 侧仍接受,供 LAN peer 用。
    req = await call({"text": "你好", "speaker": "serena",
                      "language": "english", "instruct": "用愤怒的语气"})
    check("A4 language 透传", req.language == "english", req.language)
    check("A4 instruct 透传", req.instruct == "用愤怒的语气", req.instruct)


# --------------------------------------------------------------------------- #
# B. /api/tts/voices 清单
# --------------------------------------------------------------------------- #
def test_catalog_endpoint() -> None:
    print("\n=== B. GET /api/tts/voices 清单 ===")
    import urllib.request

    try:
        with urllib.request.urlopen(f"{API_BASE}/api/tts/voices", timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        check("B0 能取到清单", False, str(e))
        return

    voices = data.get("voices") or []
    langs = data.get("languages") or []
    ref = {v["id"] for v in voices if v.get("kind") == "ref"}
    builtin = {v["id"] for v in voices if v.get("kind") == "builtin"}
    # 用户音色(上传/录制人声现场提取)也在清单里 —— 总数要算上它们。
    user = {v["id"] for v in voices if v.get("kind") == "user"}

    check("B1 参考音色 = SDK 内嵌 6 条", ref == EXPECT_REF, str(sorted(ref)))
    # 顺序跟 SDK 走:第一个是 news_female → 前端下拉默认选中它 = 历史默认
    ref_list = [v["id"] for v in voices if v.get("kind") == "ref"]
    check("B1b 参考音色第一个 = news_female(默认音色)",
          ref_list[:1] == ["news_female"], str(ref_list))
    want_builtin = _sdk_builtin()
    check("B2 内置音色 == SDK 的有效性探测结果",
          builtin == want_builtin,
          f"清单={sorted(builtin)} SDK={sorted(want_builtin)}")
    check("B3 内置音色 ⊆ 官方 9 个",
          builtin <= EXPECT_BUILTIN_MAX, str(sorted(builtin - EXPECT_BUILTIN_MAX)))
    check("B3b 音色总数 = 参考音色数 + 内置数 + 用户音色数",
          len(voices) == len(EXPECT_REF) + len(builtin) + len(user),
          str(len(voices)))

    got_lang = {l["id"] for l in langs}
    check("B4 语种 = auto + 10 种", got_lang == EXPECT_LANGUAGES,
          str(sorted(got_lang)))
    check("B5 方言不当独立语种",
          not any("dialect" in l["id"] for l in langs), str(sorted(got_lang)))

    dmap = {v["id"]: v.get("dialect", "") for v in voices if v.get("dialect")}
    check("B6 方言标记 = eric 四川话 / dylan 北京话",
          dmap == {k: v for k, v in EXPECT_DIALECT.items() if k in builtin},
          str(dmap))

    # B7: 独立复核探测器本身 —— 不信 SDK 的判定,直接读模型文件,
    # 确认「清单里出现的每个内置音色」那一行确实是训练过的(不是噪声)。
    import numpy as np
    from wdnai.backends.tts import SPK_ID, TALKER_VOCAB_SIZE, QWEN3_TTS_EMBED_DIM
    from wdnapps_audio_chat.cards.tts.service import get_session
    mdir = get_session().model_dir
    emb = np.fromfile(mdir / "talker_input_embed.fp16.bin",
                      dtype=np.float16).reshape(TALKER_VOCAB_SIZE,
                                                QWEN3_TTS_EMBED_DIM)
    norms = np.linalg.norm(emb.astype(np.float32), axis=1)
    floor = float(np.percentile(norms, 5))
    bad = [name for name in builtin
           if float(norms[SPK_ID[name]]) < 8.0 * floor]
    check("B7 清单里的内置音色行都高于噪声地板",
          not bad, f"噪声地板 {floor:.5f} 门槛 {8 * floor:.5f} 越界={bad}")


    # B8: API 必须拒掉「本模型没有」的音色 —— 否则等于给
    # 「选了却不生效」留了后门(直接 POST speaker=serena)。
    import urllib.error
    import urllib.request
    all_names = sorted(EXPECT_BUILTIN_MAX | EXPECT_REF)
    avail = ref | builtin
    missing = [n for n in all_names if n not in avail]
    for name in missing:
        try:
            req = urllib.request.Request(
                f"{API_BASE}/api/tts/start",
                data=json.dumps({"text": "测试", "speaker": name}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:  # noqa: BLE001
            check(f"B8 不可用音色 {name} 被拒", False, str(e))
            continue
        check(f"B8 不可用音色 {name} 被拒(400)", code == 400, f"HTTP {code}")


# --------------------------------------------------------------------------- #
# C. 前端静态检查
# --------------------------------------------------------------------------- #
def _card_dir() -> Path:
    """TTS 卡目录。

    卡片已拆成插件包 ``wdnapps_audio_chat`` —— 走 ``scripts/card_dirs.py`` 的
    双通道解析(内置目录 + entry point 插件包),不写死路径。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from card_dirs import card_dir

    return card_dir("tts")


def test_frontend() -> None:
    print("\n=== C. 前端静态检查 ===")
    card_dir = _card_dir()
    tpl = (card_dir / "static/template.html").read_text(encoding="utf-8")
    js = (card_dir / "static/app.js").read_text(encoding="utf-8")

    check("C1 模板有 #tts-voice 下拉", 'id="tts-voice"' in tpl)
    check("C2 模板有 #tts-language 下拉", 'id="tts-language"' in tpl)
    # instruct 输入框已下线:Base 权重没有 instruction control(官方能力表
    # Base 那一列为空),填了不改变语气,留着是误导性 UI。
    check("C3 模板已无 #tts-instruct 输入",
          'id="tts-instruct"' not in tpl and "tts-instruct" not in js)
    check("C4 旧的 #tts-ref-speaker 已移除",
          "tts-ref-speaker" not in tpl and "tts-ref-speaker" not in js)
    # 音色/语种选项全部由 /api/tts/voices 渲染 —— 模板里不该再有 <option>
    check("C5 模板里不再硬编码音色/语种选项", "<option" not in tpl)

    check("C6 前端拉清单端点", '"/tts/voices"' in js)
    check("C7 参考音色走 ref_speaker",
          "payload.ref_speaker = voice.id" in js)
    check("C8 内置音色走 speaker",
          "payload.speaker = voice.id" in js)
    # 只看 payload 的**对象字面量**(注释里提到 news_female 不算)
    m = re.search(r"const payload = \{(.*?)\n\s*\};", js, re.S)
    payload_block = m.group(1) if m else ""
    check("C9 payload 里不再硬编码 ref_speaker",
          "ref_speaker" not in payload_block and "news_female" not in payload_block,
          payload_block.strip()[:80].replace("\n", " "))

    # 标签表覆盖全部语种 —— 漏了就会在前端露出原始 id
    m = re.search(r"const LANGUAGE_LABELS = \{(.*?)\};", js, re.S)
    labels = set(re.findall(r"(\w+):", m.group(1))) if m else set()
    missing = EXPECT_LANGUAGES - labels
    check("C10 语种标签表覆盖全部 11 项", not missing, str(sorted(missing)))


def test_frontend_browser() -> None:
    """D. 浏览器实测:下拉真的渲染出来了,payload 真的分流了。

    C 段是静态检查(看源码里有那句话),挡不住「代码写了但没执行」——
    比如清单端点 404、optgroup 建错、change 事件没绑。D 段用真浏览器
    跑一遍,把这几种都堵上。
    """
    print("\n=== D. 浏览器实测 ===")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("D0 playwright 可用", False, "没装 playwright,跳过 D 段")
        return

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        pg = b.new_page()
        # ⚠️ 拦下 active-app 的写操作（DELETE 会连带清空 compute.autostart
        # ⇒ 回归脚本不该改设备级生产配置）。详见 active_app_guard.py。
        install_active_app_guard(pg)
        errs: list = []
        bad_res: list = []
        # 资源加载失败在 console 里也会冒一条 "Failed to load resource" ——
        # 那是同一件事,归到 bad_res 里按 URL 判责,别重复算。
        pg.on("console", lambda m: errs.append(m.text)
              if m.type == "error" and "Failed to load resource" not in m.text else None)
        pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
        pg.on("response", lambda r: bad_res.append(f"{r.status} {r.url}")
              if r.status >= 400 else None)
        pg.goto(f"{API_BASE}/#tts", wait_until="domcontentloaded", timeout=20000)
        pg.wait_for_timeout(2500)
        # headless 直开时 framework 的初始路由不会切到卡片页,手动切
        pg.evaluate("() => { if (window.ROUTER) ROUTER.showPage('tts'); }")
        pg.wait_for_timeout(800)

        info = pg.evaluate("""() => {
          const v = document.getElementById('tts-voice');
          const l = document.getElementById('tts-language');
          return {
            groups: v ? Array.from(v.querySelectorAll('optgroup')).map(
                g => ({label: g.label,
                      values: Array.from(g.children).map(o => o.value)})) : [],
            voiceCount: v ? v.options.length : -1,
            langCount: l ? l.options.length : -1,
          };
        }""")
        groups = info["groups"]
        by_label = {g["label"]: g["values"] for g in groups}
        want_builtin = _sdk_builtin()
        # 三类音色各一组(默认音色 / 内置音色 / 我的音色),**空的组不渲染**。
        # 断言要按**分组标题**认领,不能按下标 —— 用户音色那一组存在与否
        # 取决于用户有没有上传过,下标会随之下移。
        api_voices = _api_voices()
        want_user = {v["id"] for v in api_voices if v.get("kind") == "user"}
        want_labels = ["默认音色"]
        if want_builtin:
            want_labels.append("内置音色（模型自带）")
        if want_user:
            want_labels.append("我的音色")
        got_labels = [g["label"] for g in groups]
        check("D1 音色下拉分组与顺序正确", got_labels == want_labels,
              f"实得 {got_labels} 期望 {want_labels}")
        check("D2 参考音色组 = SDK 内嵌 6 条",
              set(by_label.get("默认音色", [])) == EXPECT_REF,
              str(by_label.get("默认音色")))
        check("D2b 参考音色组第一个 = news_female(默认选中)",
              by_label.get("默认音色", [])[:1] == ["news_female"],
              str(by_label.get("默认音色")))
        got_builtin = set(by_label.get("内置音色（模型自带）", []))
        check("D3 内置音色组 == SDK 的有效性探测结果",
              got_builtin == want_builtin,
              f"DOM={sorted(got_builtin)} SDK={sorted(want_builtin)}")
        check("D4 音色总数 = 参考音色数 + 内置数 + 用户音色数",
              info["voiceCount"] == len(EXPECT_REF) + len(want_builtin) + len(want_user),
              str(info["voiceCount"]))
        check("D4b 我的音色组 = 清单里的用户音色",
              set(by_label.get("我的音色", [])) == want_user,
              str(by_label.get("我的音色")))
        check("D5 语种数 = 11", info["langCount"] == 11, str(info["langCount"]))

        # 提示文案:只有「方言音色 + auto/chinese」才需要解释(语种会被锁定),
        # 其余情况**不提示** —— 「当前模型不含内置音色…」和「语种 auto = …」
        # 两句已删,提示不能再靠它们撑场。
        hint = pg.evaluate(
            "() => document.getElementById('tts-voice-hint').textContent")
        check("D6b 普通音色 → 提示为空", not hint.strip(), hint)

        # 方言锁定提示 —— 有真方言音色才测得着
        dialect_voice = next((v for v in sorted(want_builtin)
                              if v in EXPECT_DIALECT), None)
        if dialect_voice:
            pg.evaluate("""(voice) => {
              const v = document.getElementById('tts-voice');
              v.value = voice; v.dispatchEvent(new Event('change'));
            }""", dialect_voice)
            pg.wait_for_timeout(300)
            hint = pg.evaluate(
                "() => document.getElementById('tts-voice-hint').textContent")
            check("D6 方言音色 + auto → 提示锁定方言", "方言" in hint, hint)

        # 本板部署的是声音克隆版权重,清单里没有内置音色 → 上面的方言分支
        # 根本跑不到。造一个带 dialect 标记的探针选项验它 —— 那条提示跟
        # 音色来源无关,只看 option 上的 dialect 字段。
        pg.evaluate("""() => {
          const v = document.getElementById('tts-voice');
          const o = document.createElement('option');
          o.value = '__dialect_probe__';
          o.textContent = '方言探针';
          o.dataset.kind = 'ref';
          o.dataset.dialect = 'sichuan_dialect';
          v.appendChild(o);
          v.value = '__dialect_probe__';
          v.dispatchEvent(new Event('change'));
        }""")
        pg.wait_for_timeout(300)
        hint = pg.evaluate(
            "() => document.getElementById('tts-voice-hint').textContent")
        check("D6c 探针音色带方言 → 提示锁定方言", "方言" in hint, hint)
        # 收尾:撤掉探针,恢复默认选中(后面的 payload 分流要干净的下拉)
        pg.evaluate("""() => {
          const v = document.getElementById('tts-voice');
          const o = v.querySelector('option[value="__dialect_probe__"]');
          if (o) o.remove();
          v.value = 'news_female';
          v.dispatchEvent(new Event('change'));
        }""")
        pg.wait_for_timeout(200)

        # payload 分流(拦下 /tts/start,不发真请求)
        captured: dict = {}

        def _handler(route):
            captured.update(route.request.post_data_json or {})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"state": "loading", "busy": True}))

        pg.route("**/api/tts/start", _handler)
        # 分流要验两种音色来源:参考音色一定有;内置音色**看本模型有没有**
        # (声音克隆版权重的模型压根没有内置音色,下拉里就没有那一项)。
        cases = [("news_female", "ref")]
        builtin_first = sorted(want_builtin)[:1]
        cases += [(v, "builtin") for v in builtin_first]
        for voice, kind in cases:
            captured.clear()
            pg.evaluate("""(voice) => {
              document.getElementById('tts-text').value = '测试文本';
              const v = document.getElementById('tts-voice');
              v.value = voice; v.dispatchEvent(new Event('change'));
            }""", voice)
            pg.evaluate("() => document.getElementById('tts-start').click()")
            pg.wait_for_timeout(1500)
            if kind == "builtin":
                ok = (captured.get("speaker") == voice
                      and not captured.get("ref_speaker"))
            else:
                ok = (captured.get("ref_speaker") == voice
                      and not captured.get("speaker"))
            check(f"D7 {voice} 的 payload 分流正确", ok, str(captured))

        check("D8 页面无 JS 报错", not errs, "; ".join(errs[:3]))
        # 资源失败按 URL 归责:别的卡的接口 404 不该算成 TTS 的问题
        # (目前 /api/video-retrieval/metrics 一直是 404,VR 卡不在本仓库)。
        tts_bad = [x for x in bad_res if "/api/tts/" in x]
        check("D9 TTS 自己的请求无失败", not tts_bad, "; ".join(tts_bad[:3]))
        b.close()


def main() -> int:
    print("=== TTS 音色 / 语种清单回归 ===")
    asyncio.run(test_api_mapping())
    test_catalog_endpoint()
    test_frontend()
    test_frontend_browser()

    total = ok_count + len(failed)
    print(f"\n=== 总结: {ok_count}/{total} ===")
    if failed:
        print(f"❌ 失败: {failed}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
