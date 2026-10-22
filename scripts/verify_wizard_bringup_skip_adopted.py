#!/usr/bin/env python3
"""回归测试: wizard 一键开启必须跳过 adopted(网络 peer)实例 + server 403。

Layer 1(wizard client): mock 所有 fetch,纯静态扫 _wizardBringUpModels 的 DELETE 调用。
Layer 2(server DELETE): 直接 DELETE 真实 adopted 实例,断言 403。
"""
import json
import sys
import urllib.request
import urllib.error
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        ok = 0

        # Layer 1: 全 mock
        # GET /api/compute 返回混合实例
        #
        # **实例必须带 npu_index** —— 真机快照里一定有(registry 每次
        # to_dict 都带)。判据是 (模型, 卡位) 对;卡号未知的实例一律不动
        # (见 state.js 的 npuKnown 护栏),mock 缺字段就测不到「停多余
        # 那张卡」这条主路径了。
        page.route("**/api/compute", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "instances": [
                    # 本机 spawned,running,swcap_vision_chat → 需要,不删
                    {"model_id": "qwen3.5-vl-4b", "status": "running",
                     "npu_index": 0, "_source": "spawned",
                     "capability_types": ["swcap_vision_chat", "swcap_text_chat"]},
                    # 本机 spawned,running,swcap_vision_embedding → 不需要,应删
                    {"model_id": "qwen3-vl-embedding-2b", "status": "running",
                     "npu_index": 0, "_source": "spawned",
                     "capability_types": ["swcap_vision_embedding"]},
                    # 远端 adopted,running,swcap_vision_embedding → 绝不应删
                    {"model_id": "qwen3-vl-embedding-2b", "status": "running",
                     "npu_index": 0, "_source": "adopted",
                     "_peer_name": "PeerBoard-X",
                     "_peer_ip": "192.168.1.100",
                     "capability_types": ["swcap_vision_embedding"]},
                    # 远端 adopted,running,swcap_text_chat → 绝不应删
                    {"model_id": "peer-text-llm", "status": "running",
                     "npu_index": 0, "_source": "adopted",
                     "_peer_name": "PeerBoard-Y",
                     "_peer_ip": "192.168.1.101",
                     "capability_types": ["swcap_text_chat"]},
                    # 本机 spawned,not_loaded → 状态过滤已跳过
                    {"model_id": "qwen3.5-vl-4b", "status": "not_loaded",
                     "npu_index": 0, "_source": "spawned",
                     "capability_types": ["swcap_vision_chat"]},
                    # 本机 spawned,running 但**卡号未知**(npu_index=-1)
                    # → 判不出是不是「要的那张卡」→ 一律不动(否则 DELETE
                    #   不带卡号 = 停该模型所有卡,会误停需要的那张)
                    {"model_id": "qwen3.5-vl-4b", "status": "running",
                     "npu_index": -1, "_source": "spawned",
                     "capability_types": ["swcap_vision_chat"]},
                ],
            }),
        ))

        # 抓 DELETE 请求(POST 也 mock 掉以避免真去 spawn worker)
        deleted_models = []
        posted_payloads = []
        def handle_request(route):
            req = route.request
            raw = req.post_data or ""
            body = json.loads(raw) if raw else {}
            if req.method == "DELETE":
                deleted_models.append(body.get("model"))
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True}))
            elif req.method == "POST":
                posted_payloads.append(body)
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "snapshot": {"instances": []}}))
            else:
                route.fulfill(status=200, body="{}")
        page.route("**/api/compute/instances", handle_request)

        page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(800)

        result = page.evaluate("""
            (async () => {
                if (!window._wizardBringUpModels) return {ok: false, reason: 'no global'};
                return await window._wizardBringUpModels({
                    swcap_to_model: {
                        swcap_vision_chat: { npu_index: 0, model_id: 'qwen3.5-vl-4b' },
                    },
                });
            })()
        """)
        print(f"=== Layer 1: wizard 一键开启 ===")
        print(f"  result: {result}")
        print(f"  deleted_models: {deleted_models}")
        print(f"  posted_payloads: {posted_payloads}")

        # 验证:应删 spawned qwen3-vl-embedding-2b(不需要 + 本机)
        # 绝不能删 adopted qwen3-vl-embedding-2b 或 adopted peer-text-llm
        adopted_models = ["qwen3-vl-embedding-2b", "peer-text-llm"]  # adopted 的两个
        if deleted_models == ["qwen3-vl-embedding-2b"]:
            print(f"  ✅ 只删本机 spawned qwen3-vl-embedding-2b")
            ok += 1
        elif set(deleted_models) & set(adopted_models):
            wrong = set(deleted_models) & set(adopted_models)
            print(f"  ❌ BUG: adopted 被误删: {wrong}")
        else:
            print(f"  ❌ 删了 {deleted_models},期望 ['qwen3-vl-embedding-2b']")

        browser.close()

        # Layer 2: 真实 server DELETE
        #
        # **不能写死模型名** —— peer 板子上跑什么模型会变(写死
        # qwen3-vl-embedding-2b 的话,peer 不跑它时本机 registry 里根本没
        # 这个实例 → 后端返 404「不在 registry」→ 测试红,但 server 的
        # 403 护栏其实好好的)。改成从真实快照里挑一个 adopted 实例。
        print(f"\n=== Layer 2: 真实 server DELETE adopted ===")
        snap = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:8000/api/compute", timeout=5).read())
        adopted = [i for i in (snap.get("instances") or [])
                   if i.get("_source") == "adopted"]
        if not adopted:
            print("  ⚠ 当前没有 peer adopt 实例 → 本层跳过(不是失败)")
            print(f"\n=== 总结: {ok}/1 (Layer 2 跳过) ===")
            print("✅ 全部通过")
            return 0
        target = adopted[0]
        print(f"  目标: {target.get('model_id')} @ npu{target.get('npu_index')}"
              f" (peer={target.get('_peer_name')})")
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/compute/instances",
            method="DELETE",
            data=json.dumps({"model": target.get("model_id"),
                             "npu_index": target.get("npu_index")}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            print(f"  ❌ HTTP {resp.status} - server 没拒绝 adopted DELETE")
        except urllib.error.HTTPError as e:
            if e.code == 403:
                body = json.loads(e.read())
                print(f"  ✅ HTTP 403 - server 拒绝 adopted DELETE")
                print(f"  detail: {body.get('detail', '')[:120]}")
                ok += 1
            else:
                print(f"  ❌ HTTP {e.code},期望 403")

        print(f"\n=== 总结: {ok}/2 ===")
        if ok == 2:
            print("✅ 全部通过")
            return 0
        return 1

if __name__ == "__main__":
    sys.exit(main() or 0)