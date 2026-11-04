// rknn2 VAE 4D 探针：喂一个 .bin（float32，NCHW 逻辑序），跑一次推理，
// 把输出按 NC1HWC2 -> NCHW 归一化后存成 .bin，并可选与参考 .bin 对拍。
//
// 用法: probe_vae4d <model.rknn> <in.bin> <out.bin> [ref.bin] [core_mask]
//   in.bin  : float32，元素数 = 模型输入 n_elems（NCHW 逻辑序）
//   out.bin : float32，NCHW 逻辑序（[1,C,H,W]）
//   ref.bin : float32，HWC（[H,W,C]），与 out 比较
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include "rknn_api.h"

static const char* tn(rknn_tensor_type t) {
  switch (t) {
    case RKNN_TENSOR_FLOAT32: return "float32";
    case RKNN_TENSOR_FLOAT16: return "float16";
    case RKNN_TENSOR_INT8: return "int8";
    case RKNN_TENSOR_UINT8: return "uint8";
    case RKNN_TENSOR_INT16: return "int16";
    case RKNN_TENSOR_INT32: return "int32";
    case RKNN_TENSOR_INT64: return "int64";
    default: return "?";
  }
}
static const char* tf(rknn_tensor_format f) {
  switch (f) {
    case RKNN_TENSOR_NCHW: return "NCHW";
    case RKNN_TENSOR_NHWC: return "NHWC";
    case RKNN_TENSOR_NC1HWC2: return "NC1HWC2";
    default: return "UNDEF";
  }
}

int main(int argc, char** argv) {
  const char* model = argv[1];
  const char* inbin = argv[2];
  const char* outbin = argv[3];
  const char* refbin = (argc > 4) ? argv[4] : NULL;
  int core_mask = (argc > 5) ? atoi(argv[5]) : 7;

  rknn_context ctx = 0;
  rknn_init_extend ext;
  memset(&ext, 0, sizeof(ext));
  struct timespec t0, t1;
  clock_gettime(CLOCK_MONOTONIC, &t0);
  int ret = rknn_init(&ctx, (void*)model, 0, 0, &ext);
  clock_gettime(CLOCK_MONOTONIC, &t1);
  printf("rknn_init ret=%d (%.2f s)\n", ret,
         (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9);
  if (ret != 0) return 1;
  ret = rknn_set_core_mask(ctx, (rknn_core_mask)core_mask);
  printf("rknn_set_core_mask(%d) ret=%d\n", core_mask, ret);

  rknn_input_output_num io;
  rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io, sizeof(io));
  printf("n_input=%u n_output=%u\n", io.n_input, io.n_output);
  for (uint32_t i = 0; i < io.n_input; ++i) {
    rknn_tensor_attr a;
    memset(&a, 0, sizeof(a));
    a.index = i;
    rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &a, sizeof(a));
    printf("IN  [%u] name=%-22s dims=%u [", i, a.name, a.n_dims);
    for (uint32_t d = 0; d < a.n_dims; ++d) printf("%s%u", d ? "," : "", a.dims[d]);
    printf("] n_elems=%u size=%u type=%s fmt=%s\n", a.n_elems, a.size, tn(a.type), tf(a.fmt));
  }
  for (uint32_t i = 0; i < io.n_output; ++i) {
    rknn_tensor_attr a;
    memset(&a, 0, sizeof(a));
    a.index = i;
    rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &a, sizeof(a));
    printf("OUT [%u] name=%-22s dims=%u [", i, a.name, a.n_dims);
    for (uint32_t d = 0; d < a.n_dims; ++d) printf("%s%u", d ? "," : "", a.dims[d]);
    printf("] n_elems=%u size=%u type=%s fmt=%s\n", a.n_elems, a.size, tn(a.type), tf(a.fmt));
  }

  rknn_tensor_attr ia;
  memset(&ia, 0, sizeof(ia));
  ia.index = 0;
  rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &ia, sizeof(ia));
  FILE* f = fopen(inbin, "rb");
  if (!f) { printf("open %s failed\n", inbin); return 1; }
  float* in = (float*)malloc((size_t)ia.n_elems * sizeof(float));
  size_t got = fread(in, sizeof(float), ia.n_elems, f);
  fclose(f);
  printf("read %zu floats from %s (expect %u)\n", got, inbin, ia.n_elems);

  rknn_input rin;
  memset(&rin, 0, sizeof(rin));
  rin.index = 0;
  rin.buf = in;
  rin.size = (uint32_t)((size_t)ia.n_elems * sizeof(float));
  rin.pass_through = 0;
  rin.type = RKNN_TENSOR_FLOAT32;
  rin.fmt = RKNN_TENSOR_NCHW;
  ret = rknn_inputs_set(ctx, 1, &rin);
  printf("rknn_inputs_set ret=%d\n", ret);
  if (ret != 0) return 1;

  clock_gettime(CLOCK_MONOTONIC, &t0);
  ret = rknn_run(ctx, NULL);
  clock_gettime(CLOCK_MONOTONIC, &t1);
  printf("rknn_run ret=%d (%.1f ms)\n", ret,
         (t1.tv_sec - t0.tv_sec) * 1000.0 + (t1.tv_nsec - t0.tv_nsec) / 1e6);
  if (ret != 0) return 1;

  rknn_output rout[8];
  memset(rout, 0, sizeof(rout));
  for (uint32_t i = 0; i < io.n_output; ++i) { rout[i].index = i; rout[i].want_float = 1; }
  ret = rknn_outputs_get(ctx, io.n_output, rout, NULL);
  printf("rknn_outputs_get ret=%d\n", ret);
  if (ret != 0) return 1;

  rknn_tensor_attr oa;
  memset(&oa, 0, sizeof(oa));
  oa.index = 0;
  rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &oa, sizeof(oa));
  const uint32_t n_el = oa.n_elems;
  float* src = (float*)rout[0].buf;
  float* dst = (float*)malloc((size_t)n_el * sizeof(float));

  // 逻辑 4D [n,c,h,w] 从 native [n][c1][h][w][c2] 还原
  uint32_t n = 1, c = 1, h = 1, w = 1;
  if (oa.fmt == RKNN_TENSOR_NCHW && oa.n_dims == 4) {
    // ⭐ 逻辑 NCHW：**必须**把 dims 解析出来。
    //   （曾经漏了 ⇒ h=w=c=1 ⇒ 对拍只比了 1 个像素 ⇒ rel_L2 假到 0.0028）
    n = oa.dims[0]; c = oa.dims[1]; h = oa.dims[2]; w = oa.dims[3];
    memcpy(dst, src, (size_t)n_el * sizeof(float));
    printf("output fmt=NCHW dims=[%u,%u,%u,%u] (原样拷贝)\n",
           oa.dims[0], oa.dims[1], oa.dims[2], oa.dims[3]);
  } else if (oa.fmt == RKNN_TENSOR_NC1HWC2 && oa.n_dims == 5) {
    n = oa.dims[0]; uint32_t c1 = oa.dims[1]; h = oa.dims[2];
    w = oa.dims[3]; uint32_t c2 = oa.dims[4]; c = c1 * c2;
    for (uint32_t ni = 0; ni < n; ++ni)
      for (uint32_t ci = 0; ci < c1; ++ci)
        for (uint32_t hi = 0; hi < h; ++hi)
          for (uint32_t wi = 0; wi < w; ++wi)
            for (uint32_t cj = 0; cj < c2; ++cj)
              dst[((size_t)ni * c + ci * c2 + cj) * h * w + (size_t)hi * w + wi] =
                src[((size_t)ni * c1 * h * w * c2 + ci * h * w * c2 + hi * w * c2 +
                     wi * c2 + cj)];
    printf("converted NC1HWC2 [%u,%u,%u,%u,%u] -> NCHW [%u,%u,%u,%u]\n",
           oa.dims[0], oa.dims[1], oa.dims[2], oa.dims[3], oa.dims[4], n, c, h, w);
  } else {
    memcpy(dst, src, (size_t)n_el * sizeof(float));
    printf("output fmt=%s n_dims=%u (原样拷贝)\n", tf(oa.fmt), oa.n_dims);
  }

  FILE* fo = fopen(outbin, "wb");
  fwrite(dst, sizeof(float), n_el, fo);
  fclose(fo);
  printf("wrote %s (%u floats, %ux%u)\n", outbin, n_el, h, w);

  if (refbin) {
    FILE* fr = fopen(refbin, "rb");
    if (fr) {
      size_t rn = 0;
      fseek(fr, 0, SEEK_END);
      long rsz = ftell(fr);
      fseek(fr, 0, SEEK_SET);
      rn = (size_t)(rsz / sizeof(float));
      float* ref = (float*)malloc(rn * sizeof(float));
      fread(ref, sizeof(float), rn, fr);
      fclose(fr);
      // ref 是 HWC[H,W,C]，dst 是 NCHW[1,C,H,W]
      if (rn == (size_t)n_el) {
        double dot = 0, na = 0, nb = 0, diff = 0;
        for (uint32_t y = 0; y < h; ++y)
          for (uint32_t x = 0; x < w; ++x)
            for (uint32_t k = 0; k < c; ++k) {
              double a = dst[(size_t)k * h * w + (size_t)y * w + x];
              double b = ref[((size_t)y * w + x) * c + k];
              dot += a * b; na += a * a; nb += b * b;
              double d = a - b; diff += d * d;
            }
        printf("### board rel_L2=%.6g cosine=%.8f\n",
               sqrt(diff) / (sqrt(na) + 1e-12), dot / (sqrt(na) * sqrt(nb) + 1e-12));
      } else {
        printf("### ref 元素数 %zu != out %u，跳过对拍\n", rn, n_el);
      }
      free(ref);
    }
  }
  rknn_outputs_release(ctx, io.n_output, rout);
  rknn_destroy(ctx);
  free(in); free(dst);
  return 0;
}
