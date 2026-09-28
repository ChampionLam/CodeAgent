"""MiniMax 生视频 provider（api.minimaxi.com，Hailuo 视频）。

契约 5.5 的硬约束（全是实测踩过的坑，写死在实现里）：
  * 提交即扣配额：POST /v1/video_generation 拿 task_id —— 之后无论发生什么都不重新提交。
  * 轮询路径是 /v1/query/video_generation（**斜杠**分隔，写成下划线会 404）。
  * 轮询失败只能报 POLL_FAILED，绝不允许重新提交。
  * HTTP 200 也可能是错误：base_resp.status_code == 2056 → GenError("QUOTA")，
    立即停止，不许换模型重试、不许重新提交。
  * is_cancelled() 为真 → 停止轮询，返回 fail("CANCELLED")（已提交的任务不管）。
  * 默认间隔 10s，总上限 900s。
"""
from __future__ import annotations

import time
from typing import Callable, Sequence, Union

from gen_provider import (
    GenError,
    GenProvider,
    GenResult,
    save_bytes_to,
    BAD_REQUEST,
    CANCELLED,
    NETWORK,
    POLL_FAILED,
    PROVIDER_ERROR,
    QUOTA,
    AUTH,
    fail,
)

DEFAULT_MODEL = "MiniMax-Hailuo-2.3"
_SUBMIT_PATH = "/v1/video_generation"
_QUERY_PATH = "/v1/query/video_generation"     # 斜杠，不是下划线（下划线 404）
_RETRIEVE_PATH = "/v1/files/retrieve"
POLL_INTERVAL_S = 10.0
POLL_TOTAL_LIMIT_S = 900.0
_MAX_POLL_ERRORS = 3        # 首次 + 最多 2 次重试（同一个错误最多重试 2 次）

_STATUS_IN_QUEUE = "InQueue"
_STATUS_IN_PROGRESS = "InProgress"
_STATUS_SUCCESS = "Success"
_STATUS_FAIL = "Fail"

# _poll 的返回：成功 → file_id；失败/取消 → GenResult（ok=False）
_PollOutcome = Union[str, GenResult]


class MiniMaxVideoProvider(GenProvider):
    name = "minimax"
    kind = "video"
    api_key_env = "MINIMAX_CN_API_KEY"

    _MODELS = {
        "MiniMax-Hailuo-2.3": {
            "display": "海螺 MiniMax-Hailuo-2.3",
            "speed": "slow",
            "strengths": "文生视频（默认）",
            "default": True,
        },
    }

    def __init__(self, *, api_key: str | None = None, opener=None,
                 base_url: str | None = None, sleep=None,
                 poll_interval_s: float = POLL_INTERVAL_S,
                 poll_total_limit_s: float = POLL_TOTAL_LIMIT_S) -> None:
        super().__init__(api_key=api_key, opener=opener, sleep=sleep)
        self.base_url = (base_url or "https://api.minimaxi.com").rstrip("/")
        self.submit_url = self.base_url + _SUBMIT_PATH
        self.query_url = self.base_url + _QUERY_PATH
        self.retrieve_url = self.base_url + _RETRIEVE_PATH
        self.poll_interval_s = poll_interval_s
        self.poll_total_limit_s = poll_total_limit_s

    def models(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._MODELS.items()}

    # ---- 基础响应检查 ------------------------------------------------------

    @staticmethod
    def _check_base_resp(body: dict) -> None:
        """HTTP 200 也可能是错误：base_resp.status_code != 0 必须检查。

        2056 → GenError("QUOTA")，调用方必须立即停止（提交已扣配额）。
        """
        base_resp = body.get("base_resp") if isinstance(body, dict) else None
        if not isinstance(base_resp, dict):
            return
        sc = base_resp.get("status_code")
        if sc is None or sc == 0:
            return
        msg = base_resp.get("status_msg") or ""
        if sc == 2056:
            raise GenError(QUOTA, "MiniMax 配额不足（2056）: %s" % msg)
        if sc == 1004:
            raise GenError(AUTH, "MiniMax 鉴权失败（1004）: %s" % msg)
        if 1000 <= sc < 2000:
            raise GenError(BAD_REQUEST, "MiniMax 请求被拒（%s）: %s" % (sc, msg))
        raise GenError(PROVIDER_ERROR, "MiniMax 错误（%s）: %s" % (sc, msg))

    @staticmethod
    def _extract_task_id(body: dict) -> str | None:
        if not isinstance(body, dict):
            return None
        tid = body.get("task_id")
        if isinstance(tid, str) and tid:
            return tid
        data = body.get("data")
        if isinstance(data, dict):
            tid = data.get("task_id")
            if isinstance(tid, str) and tid:
                return tid
        return None

    # ---- 生成 --------------------------------------------------------------

    def generate(self, *, prompt: str, model: str | None, out_dir: str,
                 aspect_ratio: str | None = None, references: Sequence[str] = (),
                 params: dict | None = None,
                 is_cancelled: Callable[[], bool] = lambda: False) -> GenResult:
        def _run() -> GenResult:
            api_key = self._require_key()
            model_id = model or DEFAULT_MODEL
            if model_id not in self._MODELS:
                return fail(BAD_REQUEST, "未知模型: %s" % model_id)

            # Hailuo 视频接口没有 aspect_ratio 字段 —— 不发（发 400）。
            payload: dict = {"model": model_id, "prompt": prompt}
            duration = None
            if params:
                # 只透传白名单参数
                for k in ("duration", "prompt_optimizer"):
                    if k in params:
                        payload[k] = params[k]
                d = params.get("duration")
                if isinstance(d, (int, float)) and not isinstance(d, bool):
                    duration = d

            # ---- 提交（这一步就扣配额，之后绝不重新提交） ----
            if is_cancelled():
                return fail(CANCELLED, "提交前已取消（未发请求、未扣配额）")
            status, body = self._post_json(self.submit_url, payload, api_key=api_key)
            if status != 200 or not isinstance(body, dict):
                return fail(PROVIDER_ERROR, "提交失败 HTTP %d" % status)
            self._check_base_resp(body)     # 2056 → QUOTA，直接抛出终止（不重试不重提交）
            task_id = self._extract_task_id(body)
            if not task_id:
                return fail(PROVIDER_ERROR, "提交响应缺 task_id: %r" % (body,))

            # ---- 轮询（绝不重新提交；失败只能 POLL_FAILED） ----
            started = time.monotonic()
            outcome = self._poll(task_id, is_cancelled=is_cancelled, started=started)
            if isinstance(outcome, GenResult):     # 失败 / 取消 / 超时
                return outcome
            file_id = outcome

            # ---- 取回文件 ----
            status, body = self._get_json(self.retrieve_url + "?file_id=" + file_id,
                                          api_key=api_key, timeout=120)
            if status != 200 or not isinstance(body, dict):
                return fail(POLL_FAILED, "取回文件信息失败 HTTP %d（不重新提交）" % status)
            self._check_base_resp(body)
            download_url = (body.get("file") or {}).get("download_url")
            if not download_url:
                return fail(PROVIDER_ERROR, "取回响应缺 file.download_url: %r" % (body,))

            status, raw = self.opener(download_url, data=None, headers=None,
                                      timeout=300, method="GET")
            if status != 200:
                return fail(NETWORK, "下载视频失败 HTTP %d" % status)
            path = save_bytes_to(raw, out_dir, suffix=".mp4")

            meta = {
                "provider": self.name,
                "model": model_id,
                "prompt": prompt,
                "aspect_ratio": None,
                "raw_ids": {"task_id": task_id, "file_id": str(file_id)},
            }
            if duration is not None:
                meta["duration"] = duration
            return GenResult(ok=True, files=(path,), meta=meta)

        return self._guard(_run)

    # ---- 轮询状态机 --------------------------------------------------------

    def _poll(self, task_id: str, *, is_cancelled: Callable[[], bool],
              started: float) -> _PollOutcome:
        """轮询 task 状态：成功 → file_id；失败/取消/超时 → GenResult。

        关键不变量：本函数**绝不发提交请求**（提交即扣配额）。
        轮询请求本身出错（非 200 / 非法 JSON）时：最多重试 2 次后报 POLL_FAILED。
        """
        poll_errors = 0
        last_status = None
        while True:
            if is_cancelled():
                return fail(CANCELLED,
                            "视频生成已被取消（task_id=%s，已提交任务不再管）" % task_id)
            status, body = self._get_json(self.query_url + "?task_id=" + task_id,
                                          api_key=None, timeout=120)
            if status == 200 and isinstance(body, dict):
                self._check_base_resp(body)
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                st = body.get("status", data.get("status"))
                last_status = st
                if st == _STATUS_SUCCESS:
                    fid = body.get("file_id", data.get("file_id"))
                    if fid:
                        return str(fid)
                    return fail(POLL_FAILED,
                                "Success 但缺 file_id（task_id=%s）" % task_id)
                if st == _STATUS_FAIL:
                    return fail(POLL_FAILED, "视频生成失败（task_id=%s）" % task_id)
                # InQueue / InProgress → 继续轮询（不重试提交）
            else:
                # 轮询请求本身失败：同一次 generate 内最多重试 2 次后 POLL_FAILED
                poll_errors += 1
                if poll_errors > _MAX_POLL_ERRORS - 1:
                    return fail(POLL_FAILED,
                                "轮询请求失败 HTTP %d（task_id=%s，不重新提交）"
                                % (status, task_id))
            if (time.monotonic() - started) >= self.poll_total_limit_s:
                return fail(POLL_FAILED,
                            "轮询超时 %ss（task_id=%s，最后状态 %r，不重新提交）"
                            % (int(self.poll_total_limit_s), task_id, last_status))
            self.sleep(self.poll_interval_s)
