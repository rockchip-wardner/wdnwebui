# tools/rknn2_vae —— Qwen-Image-2.1 VAE 解码器的 **rknn2 转换 / 量化 / 验收** 流水线

VAE 解码器跑在**本机 RK3588 的 NPU**（rknn2 / RKNN2 runtime 2.5.0），
TE + DiT 仍在 2×RK1828 上（rk1820 / RKNN3）。
因为两者**物理分开**，DiT 可以全程常驻 ⇒ **生成过程零 model_init**。

模型包里的成品是 `vae_qwenimage21-512.rknn2.rknn`（int8 混合量化，整图单模型）。
这个目录放的是**怎么把它做出来**和**怎么验收它**。

---

## 1. 三个文件

| 文件 | 跑在哪 | 干什么 |
|---|---|---|
| `to4d.py` | 119（转换服务器） | **ONNX 手术**：把 5D「通道族」张量换标签成 4D 友好排布（internal 内存 -48.6% 的关键） |
| `quant_build.py` | 119 | 按 rknn-toolkit2 2.5.0 的 `config()`/`build()` 参数构建 + **模拟器对拍**（x86 上就能判精度，省得每次上板） |
| `acc_analysis.py` | 119 | rknn-toolkit2 文档里的 `accuracy_analysis` —— **逐层**精度分析（哪几层量化误差大） |
| `probe_vae4d.c` | 板子 | 板端探针：`rknn_init` + `rknn_run`（可指定核数）+ 与 ORT 参考对拍 rel_L2 / cosine |
| `accept_vae2.sh` | 板子 | 探针对拍 → 部署进模型包 → 端到端出图（2 张，验证零 model_init） |

---

## 2. 为什么需要 `to4d.py`（整件事最关键的一步）

RKNN-Toolkit2 把 **5D 张量按 NCHW 解释成 `(d0*d1, d2, d3, d4)`**：

```
ONNX  [1, 1152, 1, 32, 32]  ->  (1152, 1, 32, 32)   # C=1  ⇒ NC1HWC2 补到 C2=16 ⇒ 16 倍放大
ONNX  [1, 1, 288, 256, 256]  ->  (1, 288, 256, 256)  # C=288 ⇒ 1 倍，正常
```

VAE 里 **90% 的中间张量是 `[1, C, 1, H, W]`（D=1）** ⇒ 通道维被放大 16~32 倍。
实测 `RKNNMemStatisticsPass` 的逐算子内存表（727 条）里，
`Reshape val_637-rs` 单个就 **2304 MiB**、`view_15_sw_exSecondary` **1152 MiB**。

**修法**：把通道轴从位置 1 挪到位置 2 —— `[1,C,1,H,W]` → `[1,1,C,H,W]`。
被挪动的轴长度是 1 ⇒ **扁平下标逐字节不变**（数值逐位不变），
工具链随即按 `(1,C,H,W)` 解释 ⇒ 不再放大。

⇒ **internal 3852 MiB → 1980 MiB（-48.6%）**，总量 4335 → 2467 MiB
⇒ **第一次能装进 RK3588 的 NPU 缓冲池（实测上限 ≈ 4080 MiB）**。

脚本处理的三个难点（详见文件头 docstring）：

1. **Slice 的输出形状是机械推导的** ⇒ 改不了标签 ⇒ 在其后插恒等 `Reshape`。
2. **6D/7D「像素 shuffle 链」夹在 Conv 路径中间** ⇒ 链内部形状不可换
   ⇒ 在**链入口插「还原 Reshape」**把通道轴挪回原位。
3. **per-channel 向量 `[C,1,1,1]`** 必须同时改成 `[1,C,1,1]`，
   否则 5D 广播对齐到轴 1 ⇒ 广播出 `[1,1152,1152,32,32]`。

脚本末尾会**自检**：剥掉 value_info 后重新 shape inference，逐张量比对
⇒ 预期输出「自检通过：711 个张量的重算形状与预期完全一致」。

---

## 3. 复现流程（119 转换服务器）

模型源文件在 `/datasets/Qwen-Image-2.1`；工作目录 `rknn2_vae/`。

### 3.1 生成 4D 手术后的图

```bash
cd /datasets/Qwen-Image-2.1/rknn2_vae/onnx
TMPDIR=/datasets/tmp/rknnbuild \
  /datasets/Qwen-Image-2.1/.venv2/bin/python ../to4d.py \
  vae512_ps_g2g.onnx vae512_c2.onnx
```

> ⚠️ **TMPDIR 必须指向大盘**（`/datasets/tmp/rknnbuild`）——
> 构建过程往 `/tmp` 写 ~1.5 GB，`/` 会满。

### 3.2 构建 + 模拟器对拍

```bash
cd /datasets/Qwen-Image-2.1/rknn2_vae
nohup setsid env TMPDIR=/datasets/tmp/rknnbuild \
  /datasets/Qwen-Image-2.1/.venv2/bin/python -u quant_build.py \
    --tag c2_ah999 --onnx onnx/vae512_c2.onnx \
    --quant w8a8 --auto-hybrid --cos-thresh 0.999 \
    --dataset calib_c2/dataset.txt \
    --input-npy /datasets/tmp/vae512_in_c2.npy \
    --ref /datasets/tmp/vae512_ref.npy \
    --out rknn4/c2_ah999.rknn > logs/build_c2_ah999.log 2>&1 </dev/null & disown
```

日志尾部会打：

```
### MEM internal=1825 MiB weight=483 MiB total=2308 MiB
RESULT tag=c2_ah999 status=ok rel_L2=0.0350972 cosine=0.999389 build_s=1367
```

> ⚠️ **纯 w8a8（不加 `--auto-hybrid`）会把 VAE 彻底毁掉**：
> 输出塌成一片平色，模拟器 rel_L2 **0.715**（特征被 per-tensor int8 量化压垮）。
> `auto_hybrid` 的阈值语义是「**相似度**」——越高 ⇒ 越多层判为敏感 ⇒ 留在 fp16。
> 逐层分析（`accuracy_analysis`，412 层）显示**单层误差最大的全是 `exNorm`（GroupNorm）**
> （单层 cosine 0.086~0.089），`auto_hybrid` 正是把它们留回 fp16 的那个机制。

### 3.3 逐层精度分析（回答「哪些层该留在 fp16」）

rknn-toolkit2 文档里的 `accuracy_analysis` 会为**每一层**产出 fp32 与 qnt 两份快照
并算出量化误差 ⇒ **直接回答「哪几层量化误差大」**，也就是混合量化该把哪些层留在 fp16。

```bash
cd /datasets/Qwen-Image-2.1/rknn2_vae
nohup setsid env TMPDIR=/datasets/tmp/rknnbuild \
  /datasets/Qwen-Image-2.1/.venv2/bin/python -u acc_analysis.py \
    --tag acc1 --algorithm kl_divergence \
    --onnx onnx/vae512_ps_g2g.onnx \
    --dataset calib_real/dataset.txt \
    --inputs /datasets/tmp/vae512_in.npy > logs/acc_acc1.log 2>&1 </dev/null & disown
```

产出（实测耗时 **4h29m**）：

```
acc_acc1/error_analysis.txt   （41 KB）—— 逐层 entire（累积）/ single（单层）cosine + 欧氏距离表
acc_acc1/golden/               412 层 fp32 快照
acc_acc1/simulator/            412 层 qnt 快照
acc_acc1/map_name_to_file.txt
```

**结论：单层误差最大的全是 `exNorm`（GroupNorm）** ——
`mul_75_sw_sw` 单层 cosine **0.08939**、`add_64_rs_sw` **0.08702**、
`mul_71_sw_sw` **0.08655**；其余层基本 **> 0.98**。

⇒ **量化毁在 GroupNorm 上**，不是均匀地毁在所有层。
这正是 `auto_hybrid` 能一把救回 0.715 → 0.035 的原因。

---

## 4. 板端验收（RK3588）

```bash
# 编译探针
cd .venv/src/wdnai/src/wdnai/_cshim
g++ -O2 -std=c++14 -Iinclude -o /tmp/probe_vae4d \
    <repo>/tools/rknn2_vae/probe_vae4d.c -lrknnrt -lm

# 跑（参数：模型 输入 输出 [参考] [core_mask=7 ⇒ 3 核]）
/tmp/probe_vae4d \
    /home/rockchip/models/qwenimage21/vae_qwenimage21-512.rknn2.rknn \
    /tmp/vae512_in_c2.bin /tmp/out.bin /tmp/vae512_ref.bin 7
```

期望输出：

```
rknn_init ret=0 (1.85 s)
IN  [0] name=latents  dims=4 [1,32,32,64] n_elems=65536 size=65536 type=int8  fmt=NHWC
OUT [0] name=image    dims=4 [1,4,512,512] n_elems=1048576 size=2097152 type=float16 fmt=NCHW
rknn_run ret=0 (9557.7 ms)
### board rel_L2=0.0414524 cosine=0.99914775
```

> ⚠️ **探针的坑（已修）**：`probe_vae4d.c` 在 NCHW 分支忘了把 `oa.dims`
> 解析进 `h/w/c` ⇒ 对拍循环只比了 **1 个像素** ⇒ 报出
> `rel_L2=0.0028 / cos=1.00000000` 这种**假到离谱**的数字（真值 0.0415）。
> **读板端精度数字前先确认探针解析了 dims**，或者直接用 numpy 对拍。

> ⚠️ **板端 fp16 和模拟器 fp16 也差 1.79%**（rel_L2 0.0179 / cos 0.99984）——
> 这不是量化误差，是 **NPU 的 fp16 kernel 与模拟器（用 float32 模拟 fp16）的固有差**。
> 所以板端数字必须和这个基线一起读：
> 板端 int8 vs 板端 fp16 = **0.0414**，板端 int8 vs ORT 参考 = **0.0415**
> ⇒ 两者一致 ⇒ 模拟器与板端说的是同一件事。

### 端到端

见模型包 `README.md` §1.6。核心标志：

```
[vae-rknn2] 1 段全部常驻（生成过程零 load/init）
[qi] ⭐ VAE 走本机 rknn2（RK3588 NPU，1 段）⇒ DiT 全程常驻
```

---

## 5. 踩过的坑（勿重试）

* **探针的 dims 解析 bug** —— 见上，报出假精度。
* **`rknn_query` 在 2.5.0 是 4 参数**（没有 `n_elems_out`）。
* **`rknn_create_mem` 的 size 是 uint32_t** ⇒ 4096 MiB 会截断成 0 ⇒ 用 `rknn_create_mem2`。
* **模拟器 `rknn.inference()` 默认 `data_format='nhwc'`** ⇒ 4D 输入必须显式传 `"nchw"`。
* **工具链只给 `val_*` 命名的常量建 value info** ⇒ 新建的 shape/axes 常量必须叫 `val_9xxx`。
* **新建的 initializer 必须补 `value_info`**（含 dtype/shape），否则 fold_constant 报错。
* **int64 常量若在外部 .data 里，shape inference 读不到其值** ⇒ 自检前必须内联。
* **插入的新节点必须按拓扑序插在生产者位置** ⇒ 追加到图末尾会报 `KeyError`。
* **`onnx.save_model(save_as_external_data=True)` 在「结构已加载、权重未加载」状态下
  会把 3 GB 权重全部写丢** ⇒ 保持 `load_external_data=False` + 保留 external 引用。
* **`external_data` 是 repeated KeyValueProto**（不是 map）。
* **`id()` 不能跨阶段追踪 ONNX 节点**（protobuf 对象释放后 id 被复用）⇒ 用节点名。
* **`build()` 不消费手写的 `<model>.quantization.cfg`**；只有 `hybrid_quantization_step2` 消费它。
* **`hybrid_quantization_step1` 只有 `proposal=True` 才崩**（`KeyError: 'add_2-rs'`）。
* **`op_target` 的 key 必须是转换后的内部算子名**（`exNorm` / `exSwish` / …），
  ONNX 原名和工具链内部名（`view_15_sw_exSecondary`）都会被拒。
* **119 磁盘**：每构建一次往 `/tmp` 写 ~1.5 GB ⇒ 必须 `TMPDIR=/datasets/tmp/rknnbuild`；
  外置 .data 拷贝很慢 ⇒ 用**硬链接**。
* **本机 `/tmp` 是 tmpfs 只有 3.9 GB** ⇒ 大模型要及时 `rm`。
