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
  * 校准器必须与精度成对：fp16 用 calibrator.json（与 fp32 共用那份
    温度）；int8 必须单独拟合，套 fp32/fp16 的温度会静默失准
  * 词表一致性（2026-10-13，v6 fp16 起强制）：tokenizer 词表行数必须
    == 模型 embedding 行数，不一致直接 load failed、拒绝起 ——
    fail loud。防的就是「模型扩了词表、分词器还是旧版」的静默错位：
    fp16（22,765 行）配 v4 旧词表（21,128）不报错，但 ls -la 的
    p 实测从 0.013 漂到 0.134；方向还不定，日志里看不出来。
"""
from __future__ import annotations

import argparse
import json
import re
import sys

MAX_LENGTH = 128


def _embedding_rows(sess, np) -> int:
    """探测模型词表行数（embedding 行数）。

    不引入 onnx 包（新依赖零容忍）：用必然越界的 token id 触发
    Gather 越界错误，ORT 的错误信息自带边界
    （“idx=... must be within the inclusive range [-V,V-1]”），
    解析出 V。探测用的索引必须 > 2^31 之外的安全大数（1<<30）。
    探测失败（ORT 换了错误文案等）按行数未知处理，由调用方
    fail loud —— 宁可拒绝起，不可静默降级跑。
    """
    big = 1 << 30
    try:
        sess.run(["noul", "level", "cost"], {
            "input_ids": np.asarray([[big]], dtype=np.int64),
            "attention_mask": np.asarray([[1]], dtype=np.int64)})
    except Exception as exc:
        m = re.search(r"inclusive range \[-(\d+),(\d+)\]", str(exc))
        if m:
            return int(m.group(2)) + 1
    raise RuntimeError("cannot determine embedding rows (probe failed)")



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
        # 词表一致性断言（fail loud，见模块头）。
        rows = _embedding_rows(sess, np)
        vocab = tok.get_vocab_size()
        if rows != vocab:
            raise RuntimeError(
                "vocab mismatch: tokenizer has %d tokens but model "
                "embedding has %d rows -- model/tokenizer are NOT a "
                "matched set (e.g. v6 22,765-row model paired with the "
                "old 21,128-row tokenizer). This errors loudly by design: "
                "the mismatch would otherwise only silently skew p."
                % (vocab, rows))
    except Exception as exc:
        _load_error = "load failed: %s" % (exc,)
        print(json.dumps({"ok": False,
                          "error": _load_error}), flush=True)
        for line in sys.stdin:
            print(json.dumps({"ok": False, "error": _load_error}), flush=True)
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
