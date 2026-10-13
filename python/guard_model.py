"""DecisionBert int8 判定模型层（guard 的第二层提示层，2026-10-01）。

**极性（2026-10-01 核定，不许再反）**：serve.py 回的 p 是 noul 校准后的
sigmoid 概率，**弹卡概率** —— 高 p = 危险。上游训练定义
`noul = (action != allow)`（action ∈ {ask, block} 记 1），标签映射
ACTION_HINT = {0:"放行", 1:"弹卡"}。实测：ls -la p=0.023（安全）、
rm -rf / p=0.944、find . -delete p=0.970。阈值方向：p 越高越危险，
p >= 阈值 → 弹卡（阈值越高 → 漏拦率越高）。guard.judge_command_layered
按「p >= 0.85 弹卡且标高置信、0.35 <= p < 0.85 照样弹卡、p < 0.35 放行
入未知池」消费这个 p —— 绝不许把高 p 当成「模型很确定安全」反向用。

架构照抄 python/memory/memory_proc.py + memory_client.py 的形状：
纯逻辑 manager（注入 spawn/clock/kill，可单测）+ 真实 client
（subprocess.Popen + JSON-lines stdin/stdout + 空闲后 reap、按需
respawn、spawn 失败重试有限次后标记不可用）。

为什么模型必须住子进程：ONNX arena 在加载后不还给 OS —— int8 常驻
RSS 约 304MB，进程内加载等于永久吃掉 300MB+；子进程在空闲后 reap
可全部还给 OS，只付一次约 0.36s 冷启动。单条推理 p50 约 116ms
（本机 i5 无 GPU），每命令一条的延迟可接受。

**模型文件不入库、部署时投放**：这是公开仓，100MB 级的 ONNX 不许进
git。默认从 python/models/decision_bert/ 读（.gitignore 只忽略大体积
模型产物 *.onnx / *.onnx.data / tokenizer.json / calibrator*.json，
serve.py 与 README.md 是可跟踪代码），路径可由 config.json 的 guard
段或环境变量 DESK_GUARD_MODEL / DESK_GUARD_CALIBRATOR /
DESK_GUARD_TOKENIZER 覆盖。

版本（2026-10-13 切换）：当前分发/加载的是 **v6 fp16**（2026-10-02
训练，Windows 命令语料，词表扩到 22,765 行）。fp16 模型是 ONNX
外部数据格式：decision_bert.onnx（321,505 字节）+
decision_bert.onnx.data（205,880,832 字节，**必须同目录、都不能
改名**）。校准器用与 fp16 成对的 calibrator.json —— 套 int8 那份
（calibrator_full_v6_int8.json）不报错，只会让阈值静默失准。
词表断言：serve.py 启动时会校验 tokenizer 词表行数 == 模型
embedding 行数，不一致直接报错拒绝起（fail loud，防 v4 旧词表
静默错位 —— 实测 fp16 配 21,128 词表 ls -la 的 p 从 0.013 漂到
0.134，方向不定）。

本模块不 import onnxruntime —— 那些重依赖全在子进程脚本
python/models/decision_bert/serve.py 里，宿主进程保持轻启动。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

import guard_model_proc
import procflags
from guard_model_proc import (
    IDLE_THRESHOLD_SECONDS,
    GuardModelProcess,
    GuardModelError,
    GuardModelTimeoutError,
    GuardModelProcessDeadError,
    GuardModelProcessUnavailableError,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "models", "decision_bert", "serve.py")

# 单次推理（含冷启动握手等 ready 行）的超时。
#
# 2026-10-01 冷启动静默 fail-open 修复：旧值 5.0s 盖不住冷页缓存机型的
# 冷启动 —— 实测 fp32（406MB）冷启动墙钟 5.89s（int8 冷缓存 4.26s 也已
# 逼近），第一条 predict 在握手处超时、**一声不响地**降级回纯正则。安全
# 闸门的降级必须可观测（见 _mark_degradation / set_log_hook），阈值必须
# 盖住最慢冷启动再留余量，故提到 15.0s。注意它分别作用于握手与请求两段
# （_read_ready 与 request 各最多 timeout 秒）；热路径单条推理 p50 约
# 116ms，完全够用。
DEFAULT_TIMEOUT = 15.0

# 降级日志钩子（2026-10-01 超时可观测）：默认写 stderr 的
# ``[guard-model {level}] {msg}`` 行 —— 依赖最少、任何部署都看得到；
# 上层（sidecar）用 set_log_hook 换成自己的日志器（见 guard.warmup 的
# 接线注释）。格式刻意稳定，测试断言前缀。
_LOG_HOOK = None
_LOG_PREFIX = "[guard-model"


def _log(level: str, msg: str) -> None:
    """降级日志：默认 stderr，可被 set_log_hook 替换；永不抛。"""
    try:
        if _LOG_HOOK is not None:
            _LOG_HOOK(level, msg)
        else:
            sys.stderr.write("%s %s] %s\n" % (_LOG_PREFIX, level, msg))
            sys.stderr.flush()
    except Exception:                     # pragma: no cover - 日志永不连坐
        pass


def set_log_hook(hook) -> None:
    """替换降级日志接收器：callable(level: str, msg: str) -> None。

    传 None 恢复默认 stderr 输出。sidecar 启动时把它接到自己的 log()
    （见 guard.warmup 注释）；测试用它抓日志行做断言。
    """
    global _LOG_HOOK
    _LOG_HOOK = hook


def _read_thresholds() -> dict:
    """阈值从 config.json 的 guard 段读，缺省用默认两档。

    **极性**：p 是弹卡概率（高 p = 危险），两个阈值之上都是弹卡：
      * confident（默认 0.85）：p >= 0.85 → 弹卡且 UI 标「模型很确定」
      * ask（默认 0.35）：0.35 <= p < 0.85 → 照样弹卡（模型没把握，
        安全优先）；p < 0.35 → 放行 + 未知池回流
    绝不是「p 高 = 放行」—— 旧实现曾写反，已修正，勿回退。

    跟 appconfig.load 同一约定：config.json 优先、缺则回落
    config.example.json；模型层配置缺失不抛错，直接用默认值。
    """
    defaults = {
        "confident": 0.85,  # p >= 0.85 → 弹卡（模型高置信，UI 标「模型很确定」）
        "ask": 0.35,        # p >= 0.35 → 弹卡（默认门槛，安全优先）
                            # p < 0.35  → 放行 + 记入未知池回流
    }
    try:
        import appconfig
        cfg = appconfig.capability("guard", {}, root=_REPO_ROOT) \
            if hasattr(appconfig, "capability") else {}
        # config 顶层没有 capabilities.guard 时试顶层 guard 段
        if not cfg:
            cfg = appconfig.section("guard", {}, root=_REPO_ROOT)
    except Exception:
        return dict(defaults)
    if not isinstance(cfg, dict):
        return dict(defaults)
    out = dict(defaults)
    for key in ("confident", "ask"):
        value = cfg.get("model_%s_threshold" % key, cfg.get(key))
        if value is None:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            pass
    return out


class ChildProcess:
    """serve.py 子进程：stdin/stdout 上的换行分隔 JSON 协议。

    读取用读线程 + 队列（与 memory_client.ChildProcess 同款）：
    select() 在 Windows 的管道句柄上不可用，而这套必须跨平台。
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name="guard-model-reader")
        self._reader.start()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def _pump(self) -> None:
        try:
            for line in self._proc.stdout:            # type: ignore[union-attr]
                self._lines.put(line)
        except Exception:                              # pragma: no cover
            pass
        finally:
            self._lines.put("")                        # sentinel

    def write_line(self, text: str) -> None:
        self._proc.stdin.write(text + "\n")            # type: ignore[union-attr]
        self._proc.stdin.flush()                       # type: ignore[union-attr]

    def read_line(self, timeout: float) -> str | None:
        try:
            line = self._lines.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None
        if line == "":                                 # stdout closed
            return None
        return line.strip() or None

    def kill(self) -> None:
        try:
            self._proc.kill()
        except Exception:                              # pragma: no cover
            pass
        # kill 后 reap + 关管道：不 wait 会留僵尸（Popen.__del__ 时
        # ResourceWarning "subprocess is still running"）；不关管道则
        # 读线程持有 stdout 的 TextIOWrapper 直到线程对象被回收才散。
        try:
            self._proc.wait(timeout=5)
        except Exception:                              # pragma: no cover
            pass
        for pipe in (self._proc.stdin, self._proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:                          # pragma: no cover
                pass


class GuardModel:
    """宿主侧 client。**这里的一切都不许把异常抛进判定路径**：
    模型层挂了 → 调用方拿到 None → guard fail-open 到纯正则。
    """

    def __init__(self, *, model_path: str | None = None,
                 calibrator_path: str | None = None,
                 tokenizer_path: str | None = None,
                 python_exe: str | None = None,
                 idle_threshold: float = IDLE_THRESHOLD_SECONDS,
                 clock=time.monotonic, kill=None) -> None:
        self._model_path = model_path or default_model_path()
        self._calibrator_path = calibrator_path or default_calibrator_path()
        self._tokenizer_path = tokenizer_path or default_tokenizer_path()
        self._python = python_exe or sys.executable
        self._lock = threading.Lock()
        self._refusal = ""
        self._proc = GuardModelProcess(self._spawn, clock=clock, kill=kill,
                                       idle_threshold=idle_threshold)
        # 降级原因（可观测，2026-10-01）：None = 没降级；"unavailable" =
        # 起不来（缺文件/依赖/两次 spawn 失败）；"timeout" = 超时。
        # 两者都 fail-open，但语义不同：unavailable 是配置问题（重试也
        # 没用），timeout 是性能问题（下一轮可能就好了）——静默混在
        # 一起正是「冷启动一声不响不干活」的根源。
        self._degradation: str | None = None

    # ------------------------------------------------------------ paths
    def _spawn(self) -> ChildProcess:
        if not os.path.isfile(self._model_path):
            self._refusal = "model file not found: %s" % self._model_path
            raise GuardModelError(self._refusal)
        if not os.path.isfile(self._calibrator_path):
            self._refusal = "calibrator file not found: %s" % self._calibrator_path
            raise GuardModelError(self._refusal)
        if not os.path.isfile(self._tokenizer_path):
            self._refusal = "tokenizer file not found: %s" % self._tokenizer_path
            raise GuardModelError(self._refusal)
        try:
            proc = subprocess.Popen(
                [self._python, _SERVE_SCRIPT,
                 "--model", self._model_path,
                 "--calibrator", self._calibrator_path,
                 "--tokenizer", self._tokenizer_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
                # Windows 上不弹控制台黑窗（用户可见的两个 cmd 窗就是它）；
                # CREATE_NO_WINDOW 不改进程组/杀树语义，kill() 仍按原路
                # reap（见 ChildProcess.kill 的注释）。非 Windows 返回空
                # dict，行为零变化。
                **procflags.no_window_kwargs(),
            )
        except OSError as exc:
            self._refusal = "could not spawn guard model server: %s" % (exc,)
            raise GuardModelError(self._refusal) from exc
        # serve.py 起来就发一条 ready 行（{"ok": true, "ready": true}）；
        # 首次请求前由 _read_ready 吞掉它。起不来（依赖缺失、坏模型
        # 文件）子进程发的是 {"ok": false, ...} 原因行，同样被吞、
        # 后续请求拿到 ok:false → predict 返回 None（fail-open）。
        return ChildProcess(proc)

    @property
    def is_up(self) -> bool:
        return self._proc.is_up

    @property
    def is_unavailable(self) -> bool:
        return self._proc.is_unavailable

    @property
    def refusal(self) -> str:
        return self._refusal

    @property
    def degradation_reason(self) -> str | None:
        """最近一次降级的原因：None / "unavailable" / "timeout"。

        与 is_unavailable（生命周期标记，一旦 true 不会再尝试）互补：
        timeout 不是终态，一次成功推理会把它清掉。上层把它与日志/状态帧
        一起暴露，用户就能看到「模型层为什么没干活」。
        """
        return self._degradation

    def maybe_reap(self) -> bool:
        with self._lock:
            try:
                return self._proc.maybe_reap()
            except Exception:                          # pragma: no cover
                return False

    def shutdown(self) -> None:
        with self._lock:
            try:
                self._proc.shutdown()
            except Exception:                          # pragma: no cover
                pass

    def warmup(self, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
        """启动预热（2026-10-01 冷启动修复）：spawn + 一条哑推理。

        只该由上层在启动时的**后台线程**里调（见 guard.warmup 和
        sidecar.main 的接线）—— 把「子进程 spawn + ONNX 加载 + 首条
        推理」这几秒从第一条真实命令挪到启动期。哑命令用 "warmup ping"
        （日常命令，正则层永远放行，训练分布内安全例）。

        返回值就是那条哑推理的 predict 结果（None = 预热失败，原因看
        degradation_reason / 日志）。**永不抛**：预热失败只是退化回
        「首条命令冷启动」，不是错误 —— guard 层一切照常。
        """
        try:
            return self.predict("warmup ping", timeout=timeout)
        except Exception:                              # pragma: no cover
            return None

    # ------------------------------------------------------------ calls
    def predict(self, command: str, *, tool: str = "run_shell",
                cwd: str = "/home/user", timeout: float = DEFAULT_TIMEOUT,
                text: str | None = None) -> dict | None:
        """跑一次推理，返回 {p, level, cost} 或 None（不可用）。

        p 是**弹卡概率**（noul 校准后 sigmoid，高 p = 危险），由调用方
        （guard.judge_command_layered）按阈值决定弹卡/放行。
        绝不抛异常进判定路径：模型层挂了 → None → 正则独走。

        2026-10-01 超时可观测：返回 None 的原因记进 degradation_reason
        并走 _log（默认 stderr 的 ``[guard-model warn]`` 行，上层可用
        set_log_hook 换成自己的日志器）。**超时必须留痕** —— 冷启动
        5.9s > 旧 5.0s 阈值时静默降级，是安全闸门最危险的失败方式。
        """
        payload_text = text if text is not None else _make_text(tool, cwd, command)
        with self._lock:
            try:
                fresh = self._proc.ensure_up()
                if not self._proc.is_up:
                    self._mark_degradation("unavailable", self._refusal or
                                           "spawn failed twice; guard model layer unavailable")
                    return None
                if fresh:
                    self._read_ready(timeout)
                reply = self._proc.request(
                    {"text": payload_text}, timeout)
                if isinstance(reply, dict) and "ready" in reply:
                    # 迟到的 ready 行（握手超时后才到）：这条应答不属于
                    # 本请求，应答流已错位 —— 丢弃子进程强制下次 respawn，
                    # 绝不把错位的下一条 p 当真（desync 的兜底闸）。
                    self._proc.shutdown()
                    self._mark_degradation(
                        "unavailable",
                        "stale ready line: response stream misaligned; child discarded")
                    return None
            except GuardModelTimeoutError as exc:
                # 活着的子进程不回话 → 超时。与「起不来」不同：这次没值
                # 不代表下次没值（冷缓存挤掉后重试可能就过了），所以必须
                # 明确留痕而不是只返回 None。
                self._mark_degradation(
                    "timeout",
                    "guard model inference timed out after %.1fs (child alive, "
                    "no reply) -- degrading to regex-only for this command; "
                    "cold-cache machines can exceed this on first spawn" % timeout,
                    detail=str(exc))
                return None
            except GuardModelError as exc:
                # 死子进程 / 坏应答等：记录但 fail-open（与旧行为一致，
                # 只是补了观测）。
                self._mark_degradation("unavailable", str(exc))
                return None
            except Exception as exc:
                self._mark_degradation("unavailable",
                                       "guard model layer failed: %r" % (exc,))
                return None
        if reply.get("ok") is not True:
            self._mark_degradation("unavailable",
                                   "guard model server rejected request: %s"
                                   % (reply.get("error") or reply,))
            # 起不来属于配置问题（模型/校准器/词表不匹配、依赖缺失），
            # 重试也不会好 —— 标记 unavailable 让生命周期不再空转。
            self._proc.shutdown()
            return None
        try:
            out = {
                "p": float(reply["p"]),
                "level": int(reply["level"]),
                "cost": int(reply["cost"]),
            }
        except (KeyError, TypeError, ValueError):
            self._mark_degradation("unavailable",
                                   "malformed guard model reply: %r" % (reply,))
            return None
        self._degradation = None      # 成功一次：降级标记清零
        return out

    def _mark_degradation(self, reason: str, message: str,
                          detail: str | None = None) -> None:
        """记一次降级原因并发日志（永不抛）。"""
        self._degradation = reason
        _log("warn", "model layer degraded (%s): %s%s"
             % (reason, message, (" [%s]" % detail) if detail else ""))

    def _read_ready(self, timeout: float) -> None:
        """**只读不发**：吞掉 serve.py 启动后自发的 ready 行。

        2026-10-01 desync 修复：旧实现用 ``self._proc.request({})`` 做
        握手 —— 但 serve.py 对空请求对象也照常跑一条推理并回一条应答，
        于是宿主发出的第一条真请求的应答队列里躺着两条应答（空请求的
        + 真请求的），读走的是空请求那条 → **此后每条命令拿到的都是
        前一条的 p**（实测：predict('ls -la') 拿 0.627 = 空请求的读数，
        而真实 p('ls -la')=0.023）。修复：握手只 ``read_line`` 消费
        serve.py 主动发的 ready 行，不往 stdin 写任何东西，请求/应答
        流从此一一对应。

        ready 行本身携带可用性信息：deps/load 失败时 serve.py 发的是
        ``{"ok": false, "error": ...}`` 原因行 —— 读完即可判不可用；
        超时读不到行则放弃握手（子进程稍后自然 dead，fail-open 兜底）。
        """
        child = getattr(self._proc, "_child", None)
        if child is None:
            return
        try:
            child.read_line(timeout)
        except Exception:
            pass


def _make_text(tool: str, cwd: str, command: str) -> str:
    """输入契约（必须与训练逐字一致，漂移只静默掉分不报错）：
    [工具] {tool} [目录] {cwd} [命令] {command}，tool 固定 run_shell、
    cwd 固定 /home/user（训练时的 DEFAULT_TOOL / DEFAULT_CWD）。
    """
    return f"[工具] {tool} [目录] {cwd} [命令] {command}"


# ---------------------------------------------------------------- paths
def default_model_path() -> str:
    """模型 ONNX 默认位置（仓库内 gitignore 目录，部署时投放）。

    v6 fp16：decision_bert.onnx 是外部数据格式，真正的权重在旁边的
    decision_bert.onnx.data（同目录、不改名）。这里只返回 .onnx 的
    路径；.data 文件缺失时 serve.py 加载会报 load failed。
    """
    return os.environ.get("DESK_GUARD_MODEL") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "models", "decision_bert", "decision_bert.onnx")


def default_calibrator_path() -> str:
    """与 fp16 成对的校准器（套 int8 那份会静默失准，见模块头）。"""
    return os.environ.get("DESK_GUARD_CALIBRATOR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "models", "decision_bert", "calibrator.json")


def default_tokenizer_path() -> str:
    """BERT 中文 tokenizer.json（tokenizers 库直接加载）。"""
    return os.environ.get("DESK_GUARD_TOKENIZER") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "models", "decision_bert", "tokenizer.json")


def threshold_values() -> dict:
    """当前生效的三档阈值（测试与展示用）。"""
    return _read_thresholds()


__all__ = [
    "GuardModel", "GuardModelError", "default_model_path",
    "default_calibrator_path", "default_tokenizer_path",
    "threshold_values", "IDLE_THRESHOLD_SECONDS", "DEFAULT_TIMEOUT",
    "set_log_hook",
]
