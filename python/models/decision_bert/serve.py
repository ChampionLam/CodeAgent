#!/usr/bin/env python3
"""DecisionBert int8 推理子进程（guard 第二层的服务端）。

宿主进程不 import onnxruntime —— 重依赖全锁在这个子进程里，宿主保持
轻启动、且模型层挂了不影响主流程。协议：stdin 每行一个 JSON 请求
{"text": "..."}，stdout 每行一个 JSON 应答 {"ok": true, "p": ..,
"level": .., "cost": ..}。出错写 {"ok": false, "error": "..."}，不崩
进程（崩了宿主只会把它当 dead respawn，信息就丢了）。

为什么驻留而不是每次 fork 一次：ONNX arena 加载后不还给 OS，常驻
RSS ~304MB，reap 一次全部还给 OS；单条推理 p50 ~116ms，比进程冷启动
一次再推理便宜得多。

输入/输出契约（必须与训练逐字一致，漂移只静默掉分不报错）：
  * 输入 text = "[工具] run_shell [目录] /home/user [命令] {command}"
    （DEFAULT_TOOL / DEFAULT_CWD，为训练时固定的常量，不可改）
  * BERT 中文 tokenizer，max_length=128，truncation
  * ONNX 三个输出都是 logit：noul[1] / level[1,4] / cost[1,3]
  * 校准顺序不能反：先 logit / T[head] 再激活 —— noul 用 sigmoid，
    level/cost 用 softmax；level/cost 只做展示，不参与自动决策
  * 校准器必须用 int8 专属 calibrator_full_v4_int8.json（配 fp32 那份
    阈值会静默失准：level ECE 0.0470→0.0553）
"""
from __future__ import annotations

import argparse
import json
import sys

MAX_LENGTH = 128


def _load_calibrator(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {h: float(v["temperature"])
            for h, v in data["heads"].items()}


def _softmax(xs):
    import math
    m = max(xs)
    exps = [math.exp(x - m) for x in xs]
    total = sum(exps)
    return [e / total for e in exps]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--calibrator", required=True)
    ap.add_argument("--tokenizer", required=True)
    args = ap.parse_args()

    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except Exception as exc:
        print(json.dumps({"ok": False,
                          "error": "deps missing: %s" % (exc,)}), flush=True)
        # 依赖缺失：继续读循环但永远回答不可用，宿主会判 unavailable。
        for line in sys.stdin:
            print(json.dumps({"ok": False, "error": "deps missing"}), flush=True)
        return 0

    try:
        sess = ort.InferenceSession(
            args.model, providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(args.tokenizer)
        temps = _load_calibrator(args.calibrator)
    except Exception as exc:
        print(json.dumps({"ok": False,
                          "error": "load failed: %s" % (exc,)}), flush=True)
        for line in sys.stdin:
            print(json.dumps({"ok": False, "error": "load failed"}), flush=True)
        return 0

    # 预热一条，把首条推理的延迟从冷启动里剥出来（arena / 线程池就绪）。
    try:
        enc = tok.encode("[工具] run_shell [目录] /home/user [命令] ls")
        _infer(sess, np, enc)
        print(json.dumps({"ok": True, "ready": True}), flush=True)
    except Exception:
        pass

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            text = str(req.get("text", ""))
            enc = tok.encode(text)
            noul, level, cost = _infer(sess, np, enc)
            # 校准：先除温度，再激活。
            p_noul = float(1.0 / (1.0 + math.exp(-noul / temps.get("noul", 1.0))))
            p_level = _softmax([x / temps.get("level", 1.0) for x in level])
            p_cost = _softmax([x / temps.get("cost", 1.0) for x in cost])
            print(json.dumps({
                "ok": True,
                "p": round(p_noul, 6),
                "level": int(max(range(len(level)), key=level.__getitem__)),
                "cost": int(max(range(len(cost)), key=cost.__getitem__)),
                "p_level": [round(x, 4) for x in p_level],
                "p_cost": [round(x, 4) for x in p_cost],
            }), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
    return 0


def _infer(sess, np, enc):
    ids = enc.ids[:MAX_LENGTH]
    mask = enc.attention_mask[:MAX_LENGTH]
    input_ids = np.asarray([ids], dtype=np.int64)
    attention_mask = np.asarray([mask], dtype=np.int64)
    out = sess.run(["noul", "level", "cost"],
                   {"input_ids": input_ids, "attention_mask": attention_mask})
    noul = float(out[0][0])
    level = [float(x) for x in out[1][0]]
    cost = [float(x) for x in out[2][0]]
    return noul, level, cost


if __name__ == "__main__":
    import math
    sys.exit(main())
