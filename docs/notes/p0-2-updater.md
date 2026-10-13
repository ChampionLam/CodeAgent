# CodeAgent 程序更新功能（P0-2，t_db5df87b）

按 docs/design-agent-intelligence.md §3 的 P0 口径实现，落在 Python 层
（python/updater.py + sidecar update.* RPC）。Electron 层接线在后续任务。

## 设计口径（与设计文档一致）

- 更新源：URL（https:// 或 http://，国内可达地址可配）或本地目录。
  latest.json 放在源根下（URL 源为 `<baseUrl>/latest.json`，本地源为
  `<dir>/latest.json`）。
- 版本比较：自写 semver-ish 比较（数值段优先、非数值段字典序，`1.0.0-beta`
  与 `1.0.0` 可比）。不引第三方依赖（requirements 只有 Pillow）。
- 静默降级：update.check 任何失败都返回 `available: false` + `error` 字符串
  并记 WARN 日志，不抛异常到 RPC 层之外，不打扰用户。
- 下载与三重校验（校验不过一律拒绝安装）：
  1. sha256 摘要核对（latest.json 里 files[platform].sha256，必填；
     sha256 字段缺失也按失败处理）
  2. 体积区间（min 1 字节，max 默认 500MB，可配 maxBytes）
  3. 摘要一致性：latest.json 的 version 字段必须在文件名或 staged
     package 的 package.json version 里出现（防 latest.json 与包内容错位）
- 有签名就验签名（本 P0 无签名体系，字段预留，出现则校验失败因为无证书；
  见 verify 管道 signature 字段）。
- 原子替换与回滚：
  - 下载/复制到 `updates/` 下临时目录，完整校验通过后 stage
  - apply：旧安装目录 rename 成 `install-dir.bak-<ts>`，staged rename 进
    原位置；任一步失败反向 rename 回滚并把 staged 标记 failed
  - 跨卷 rename 失败退化为 copy+swap（先全量 bak 再切换）
- 数据不变性：apply 前检查 zip 内条目，黑名单路径（data/、config*.json、
  .env、sessions.db、skills/）出现在包里 → 拒绝安装（WITH_USER_DATA 错误）
- 离线包：update.applyLocal {path}，path 可为 zip 或目录。目录先打 zip 再
  走与在线完全相同的校验管道（单一校验逻辑）。zip 里的 latest.json/sha256
  可以缺失（离线包没有远端 manifest），此时体积校验照做、sha256 若旁边有
  `<path>.sha256` 或包内 `latest.json` 提供则核对，否则跳过 sha256 但记录
  `verified: ["size"]`。
- 更新日志：所有结果写 logging_kit（source="updater"），落 P0-1 的 JSONL
  日志文件。
- 版本自证：apply 成功后读新安装目录 package.json 的 version 并与
  期望版本比对，结果写进 apply 记录（mismatch 不回滚——文件已切换，只
  诚实报告，让用户决定；设计文档要求的是「能自证」，不是强一致）。

## RPC（sidecar.py 分发，前缀 update.）

- update.status → {currentVersion, channel, source, sourceType, updatesDir}
- update.check → {available, current, latest?, notes?, error?}
  （失败：available=false + error 字符串，绝不抛错）
- update.download → {ok, path?, sizeBytes?, error?}
  （先 check；把选中平台的包下到 updates/download-<version>.zip）
- update.applyLocal {path} → {ok, applied, oldVersion, newVersion,
  backupPath, error?, appliedFiles?}
- update.apply（download 后应用）→ 同 applyLocal 返回结构

## 文件

- python/updater.py — 全部逻辑，模块级函数 + UpdateSource 类型，可独立
  import 测试；cli 入口 `python3 python/updater.py apply-local <path>`
  供端到端脚本验证（真机路径标注「未真机验证」）
- python/tests/test_updater.py — 单测：版本比较、check 失败静默、
  sha256 伪造被拒、数据目录不动、替换失败回滚、离线目录包端到端、
  RPC 分发（mock sidecar 路由）

## 未做 / 标注

- Electron UI（「检查更新」按钮、autoCheck 定时）→ 后续任务
- 断点续传（P1）、签名验签（P2，等签名证书）
- Windows 真机占用 rename 重试（updater.py 的 _apply 有 retry=3 的
  rename 循环，但未在 Windows 真机验证）
