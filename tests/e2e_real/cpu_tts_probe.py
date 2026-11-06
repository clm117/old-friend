# -*- coding: utf-8 -*-
"""CPU TTS 真实合成探针

直接调用产品代码 ai-service/voxcpm2/voxcpm2_service.py 中的 VoxCPM2Service，
加载本地 models/voxcpm2 并合成一句短文本，记录加载耗时、合成耗时、音频时长与 RTF。

说明：
- 不使用 mock，不依赖 DashScope。
- 不走 HTTP 服务，是因为 TTS_PROVIDER=voxcpm2 时 ai-service 启动会实例化
  FunAudioChatService（8B，fp16 约 16GB），本机 16GB 内存无法承受；详见报告。
- 结果写入同目录 runtime/cpu_tts_result.json。
"""
import argparse
import io
import json
import os
import sys
import time
import traceback
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "ai-service"
sys.path.insert(0, str(AI_DIR))

RUNTIME = Path(__file__).resolve().parent / "runtime"
RUNTIME.mkdir(parents=True, exist_ok=True)


def mem_mb():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {"total_mb": round(vm.total / 1048576), "available_mb": round(vm.available / 1048576)}
    except Exception:
        return {}


def peak_rss_mb():
    try:
        import psutil
        return round(psutil.Process(os.getpid()).memory_info().rss / 1048576)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="你好，很高兴认识你")
    ap.add_argument("--quick", action="store_true", default=True)
    ap.add_argument("--out", default=str(RUNTIME / "cpu_tts_result.json"))
    ap.add_argument("--workaround", action="store_true",
                    help="诊断用：将 SDPA 的 1D attn_mask 补成 [1,1,1,N]，绕过 torch 2.15 维度报错")
    args = ap.parse_args()

    result = {
        "case": "CPU-TTS-DIRECT",
        "text": args.text,
        "quick_mode": args.quick,
        "python": sys.version,
        "model_path": str(ROOT / "models" / "voxcpm2"),
        "mem_before": mem_mb(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "FAIL",
    }
    t_all = time.time()

    try:
        import torch
        result["torch"] = torch.__version__
        result["cuda_available"] = bool(torch.cuda.is_available())
        if args.workaround:
            # 诊断补丁：voxcpm 的 MiniCPM4 forward_step 传入 1D attn_mask，
            # torch 2.15 的 SDPA 不再接受该形状。仅补形状，不改模型/权重/推理参数。
            _orig_sdpa = torch.nn.functional.scaled_dot_product_attention

            def _sdpa_compat(query, key, value, *a, **kw):
                am = kw.get("attn_mask")
                if am is not None and getattr(am, "dim", lambda: 0)() == 1:
                    kw["attn_mask"] = am.view(1, 1, 1, -1)
                return _orig_sdpa(query, key, value, *a, **kw)

            torch.nn.functional.scaled_dot_product_attention = _sdpa_compat
            result["workaround"] = "SDPA 1D attn_mask -> [1,1,1,N]（仅诊断用，非产品默认路径）"
        from voxcpm2.voxcpm2_service import VoxCPM2Service
        svc = VoxCPM2Service()
        result["device"] = svc.device

        t0 = time.time()
        ok = svc._ensure_model()
        result["load_seconds"] = round(time.time() - t0, 2)
        result["load_ok"] = bool(ok)
        result["mem_after_load_mb"] = mem_mb()
        if not ok:
            result["error"] = "VoxCPM2 模型加载失败（_ensure_model 返回 False）"
            return finish(result, args.out, t_all)

        t1 = time.time()
        wav_bytes, sample_rate = svc.synthesize(
            args.text, quick_mode=args.quick
        )
        synth_elapsed = time.time() - t1
        result["synth_seconds"] = round(synth_elapsed, 2)
        result["peak_rss_mb"] = peak_rss_mb()
        result["mem_after_synth_mb"] = mem_mb()

        if not wav_bytes:
            result["error"] = "synthesize 返回空音频"
            return finish(result, args.out, t_all)

        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            duration = wf.getnframes() / float(wf.getframerate())
            result["audio"] = {
                "bytes": len(wav_bytes),
                "sample_rate": wf.getframerate(),
                "channels": wf.getnchannels(),
                "sampwidth": wf.getsampwidth(),
                "duration_seconds": round(duration, 3),
            }
        out_wav = RUNTIME / "cpu_tts.wav"
        out_wav.write_bytes(wav_bytes)
        result["wav_path"] = str(out_wav)

        result["rtf"] = round(synth_elapsed / duration, 2) if duration else None
        result["first_byte_seconds"] = result["synth_seconds"]  # 非流式：返回整段音频
        result["total_seconds"] = round(time.time() - t_all, 2)
        result["status"] = "PASS" if duration > 0 and len(wav_bytes) > 1000 else "FAIL"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()[-1500:]
    return finish(result, args.out, t_all)


def finish(result, out_path, t_all):
    result["total_seconds"] = round(time.time() - t_all, 2)
    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
