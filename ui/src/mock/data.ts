import type {
  Session, Message, ModelConfig, PermissionRule
} from '../types';

const now = Date.now();
const min = 60_000;
const hr = 60 * min;

export const mockSessions: Session[] = [
  {
    id: 's-1',
    title: '重构用户服务层',
    createdAt: now - 3 * hr,
    updatedAt: now - 4 * min,
    modelId: 'm-deepseek',
    pinned: true
  },
  {
    id: 's-2',
    title: '分析销售数据 Q3',
    createdAt: now - 28 * hr,
    updatedAt: now - 22 * hr,
    modelId: 'm-claude'
  },
  {
    id: 's-3',
    title: 'React Hook 单元测试',
    createdAt: now - 2 * 24 * hr,
    updatedAt: now - 2 * 24 * hr + 35 * min,
    modelId: 'm-gpt4o'
  },
  {
    id: 's-4',
    title: '设计缓存淘汰策略',
    createdAt: now - 4 * 24 * hr,
    updatedAt: now - 4 * 24 * hr + 2 * hr,
    modelId: 'm-deepseek'
  },
  {
    id: 's-5',
    title: '翻译技术文档：gRPC 入门',
    createdAt: now - 6 * 24 * hr,
    updatedAt: now - 5 * 24 * hr,
    modelId: 'm-claude'
  },
  {
    id: 's-6',
    title: 'SQL 索引优化建议',
    createdAt: now - 8 * 24 * hr,
    updatedAt: now - 8 * 24 * hr + 15 * min,
    modelId: 'm-gpt4o'
  }
];

/**
 * The default session. Deliberately exercises every render path in one screen:
 * markdown headings / lists / table / blockquote / task list / inline code,
 * a highlighted TypeScript code block, and all five tool-call states
 * (running, awaiting, success, failed, rejected).
 */
export const mockLiveMessages: Message[] = [
  {
    id: 'msg-1',
    role: 'user',
    content: '帮我看看 src/api/users.ts 里保存用户那段，另外缓存层能不能一起抽出来？',
    createdAt: now - 6 * min
  },
  {
    id: 'msg-2',
    role: 'assistant',
    content: '好，我先把相关文件读出来看结构。',
    createdAt: now - 5.6 * min,
    toolCalls: [
      {
        id: 'tc-read',
        name: 'read_file',
        summary: 'Read · src/api/users.ts',
        level: 'L1',
        status: 'success',
        args: { path: 'src/api/users.ts', startLine: 0, maxLines: 400 },
        result: `export async function saveUser(input: SaveUserInput) {
  const user = normalize(input);
  const existing = await db.users.findByEmail(user.email);
  if (existing) throw new DuplicateEmailError(user.email);
  const id = await db.users.insert(user);
  await events.publish('user.created', { id, email: user.email });
  return { id, ...user };
}`,
        startedAt: now - 5.6 * min,
        endedAt: now - 5.55 * min
      },
      {
        id: 'tc-write',
        name: 'write_file',
        summary: 'Write · app/core/cache.py',
        level: 'L2',
        status: 'success',
        args: {
          path: 'app/core/cache.py',
          content: `"""Process-local TTL cache."""
from cachetools import TTLCache

_cache: TTLCache[str, object] = TTLCache(maxsize=2048, ttl=60)


def invalidate(prefix: str) -> None:
    for key in list(_cache.keys()):
        if key.startswith(prefix):
            del _cache[key]
`
        },
        result: '已写入 14 行（新建文件）。',
        startedAt: now - 5.4 * min,
        endedAt: now - 5.38 * min
      },
      {
        id: 'tc-rejected',
        name: 'bash',
        summary: 'Bash · rm -rf ./build',
        level: 'L3',
        status: 'rejected',
        args: { command: 'rm -rf ./build' },
        result: '已拒绝：L3 级危险操作需要逐次人工确认，未被授权。',
        startedAt: now - 5.2 * min,
        endedAt: now - 5.19 * min
      },
      {
        id: 'tc-failed',
        name: 'bash',
        summary: 'Bash · pytest -q tests/cache',
        level: 'L1',
        status: 'failed',
        args: { command: 'pytest -q tests/cache' },
        result: `FAILED tests/cache/test_cache.py::test_ttl_evict
  AssertionError: ttl=60s but entry expired after 0.03s (clock skew)
1 failed, 4 passed in 0.42s`,
        startedAt: now - 5.0 * min,
        endedAt: now - 4.94 * min
      },
      {
        id: 'tc-awaiting',
        name: 'edit_file',
        summary: 'Edit · src/api/users.ts · 提议重构',
        level: 'L2',
        status: 'awaiting',
        args: {
          path: 'src/api/users.ts',
          explanation: '把竞态检查包进事务，并提前到行级守卫位置。'
        },
        startedAt: now - 4.9 * min
      },
      {
        id: 'tc-running',
        name: 'grep',
        summary: 'Grep · cachetools 引用点',
        level: 'L1',
        status: 'running',
        args: { pattern: 'cachetools|_cache\\\[', glob: '**/*.py' },
        startedAt: now - 4.7 * min
      }
    ]
  },
  {
    id: 'msg-3',
    role: 'user',
    content: '可以，把方案写出来我看看，顺便给个兜底策略。',
    createdAt: now - 4 * min
  },
  {
    id: 'msg-4',
    role: 'assistant',
    content: `读完了，问题集中在三处，我按优先级从高到低说。

## 1. 竞态：检查与写入不在同一事务

\`findByEmail\` 和 \`insert\` 之间没有事务边界，两个并发请求可能同时通过校验。修法是把校验下沉到唯一索引，让数据库兜底：

\`\`\`typescript
export async function saveUser(input: SaveUserInput): Promise<User> {
  const user = normalize(input);
  try {
    const id = await db.users.insert(user);   // 唯一索引撞了会抛
    return { id, ...user };
  } catch (err) {
    if (isUniqueViolation(err, 'users_email_key')) {
      throw new DuplicateEmailError(user.email);
    }
    throw err;
  }
}
\`\`\`

## 2. 缓存层：本地 TTL + 主动失效

缓存策略建议分两层考虑：

| 层级 | 存储 | 命中率 | 失效方式 |
| --- | --- | --- | --- |
| L1 | 进程内 TTLCache | 高 | TTL 到期自动淘汰 |
| L2 | Redis | 中 | 写入时按前缀主动清除 |

- **L1 只放热点读**，容量给到 2048 条、TTL 60 秒
- **写路径必须主动失效**，不能等 TTL 自然过期，否则读到陈旧数据
- 失效用前缀批量删除（\`invalidate("user:")\`），避免逐个 key 扫描

> 兜底原则：缓存失效策略宁可是"多清一点"，也不要"漏清"。多清只是多打几次数据库，漏清会直接给出错误数据。

## 3. 待办

- [x] 抽出 \`app/core/cache.py\`
- [ ] 给 \`users.email\` 补唯一索引迁移
- [ ] 写并发写入的回归测试
- [ ] 把 \`events.publish\` 改成 outbox 模式异步重试

需要我先把唯一索引的迁移脚本生成出来吗？`,
    createdAt: now - 3.5 * min
  },
  {
    id: 'msg-5',
    role: 'user',
    content: '好，先出迁移脚本，顺便把 outbox 的骨架也列一下。',
    createdAt: now - 40_000
  },
  {
    id: 'msg-6',
    role: 'assistant',
    content: `好，我按顺序来。先写迁移脚本`,
    streaming: true,
    createdAt: now - 30_000
  }
];

// ---------------------------------------------------------------------------
// Seed histories for s-2 .. s-6 — each conversation is intentionally distinct
// so clicking between them in the sidebar shows different content.
// ---------------------------------------------------------------------------

/** s-2 · 销售 Q3 分析 */
const s2Messages: Message[] = [
  {
    id: 's2-m1',
    role: 'user',
    content: '把 q3-sales.csv 里的数据按区域分组，告诉我哪个区域增长最快、哪个下滑最严重。',
    createdAt: now - 22 * hr
  },
  {
    id: 's2-m2',
    role: 'assistant',
    content: '我先把表读出来跑个聚合，然后按区域对比环比。',
    createdAt: now - 22 * hr + 20_000,
    toolCalls: [
      {
        id: 's2-tc-read',
        name: 'bash',
        summary: 'Bash · csvstat q3-sales.csv',
        level: 'L1',
        status: 'success',
        args: { command: 'csvstat --columns region,amount q3-sales.csv' },
        result: `Row count: 4_812
Columns: region (5 unique), amount (mean=¥18_420, std=¥12_310)
Missing: 0`,
        startedAt: now - 22 * hr + 20_000,
        endedAt: now - 22 * hr + 22_000
      },
      {
        id: 's2-tc-py',
        name: 'bash',
        summary: 'Bash · python q3_growth.py',
        level: 'L2',
        status: 'success',
        args: { command: 'python scripts/q3_growth.py --by region' },
        result: '已生成 report/q3_growth_by_region.csv 与 chart/q3_growth.png',
        startedAt: now - 22 * hr + 24_000,
        endedAt: now - 22 * hr + 90_000
      }
    ]
  },
  {
    id: 's2-m3',
    role: 'user',
    content: '华东涨得猛是因为大客户续约，还是渠道下沉？',
    createdAt: now - 22 * hr + 2 * min
  },
  {
    id: 's2-m4',
    role: 'assistant',
    content: `拆开看，主要是**渠道下沉**贡献的。华东 Q3 同比增长 38%，其中：

- 头部 20 个客户贡献 +9 个百分点（续约稳定，但没拉动大盘）
- 中长尾经销商贡献 +22 个百分点（新签 47 家，单店产出 ¥6.8k）
- 直营门店贡献 +7 个百分点（开新城 2 个，但单店产能没变）

反观华南，掉了 11 个百分点，问题集中在**两个老渠道商同时转竞品**，需要先和他们聊清楚返点结构。

## 建议优先级

1. 立刻：约华南两家渠道商，明确 Q4 返点 + 独家保护期
2. 一周内：把华东的渠道下沉 SOP 复制到华中和西南
3. 月底前：把头部客户的健康度看板搭出来，避免下次被动

要不要我先把华南那两家过去三年的合同和返点记录拉出来，整理一份谈判素材？`,
    createdAt: now - 22 * hr + 5 * min
  }
];

/** s-3 · React Hook 单元测试 */
const s3Messages: Message[] = [
  {
    id: 's3-m1',
    role: 'user',
    content: '给 useDebouncedValue 写一组单元测试，要覆盖正常用例、计时器清理、组件卸载。',
    createdAt: now - 2 * 24 * hr
  },
  {
    id: 's3-m2',
    role: 'assistant',
    content: '好，先确认一下 hook 的实现再决定怎么测。',
    createdAt: now - 2 * 24 * hr + 30_000,
    toolCalls: [
      {
        id: 's3-tc-read',
        name: 'read_file',
        summary: 'Read · src/hooks/useDebouncedValue.ts',
        level: 'L1',
        status: 'success',
        args: { path: 'src/hooks/useDebouncedValue.ts' },
        result: `import { useEffect, useState } from 'react';

export function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}`,
        startedAt: now - 2 * 24 * hr + 30_000,
        endedAt: now - 2 * 24 * hr + 32_000
      }
    ]
  },
  {
    id: 's3-m3',
    role: 'user',
    content: '实现没问题，直接用 @testing-library/react 的 renderHook，配合 fake timers。',
    createdAt: now - 2 * 24 * hr + 2 * min
  },
  {
    id: 's3-m4',
    role: 'assistant',
    content: `下面是 \`useDebouncedValue.test.tsx\`，三个用例覆盖到位：

\`\`\`tsx
import { renderHook, act } from '@testing-library/react';
import { useDebouncedValue } from '../useDebouncedValue';

describe('useDebouncedValue', () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  it('returns the value after the delay elapses', () => {
    const { result } = renderHook(() => useDebouncedValue('a', 200));
    expect(result.current).toBe('a');
    act(() => { jest.advanceTimersByTime(199); });
    expect(result.current).toBe('a');
    act(() => { jest.advanceTimersByTime(1); });
    expect(result.current).toBe('a');
  });

  it('only commits the latest value when input changes rapidly', () => {
    const { result, rerender } = renderHook(
      ({ v }) => useDebouncedValue(v, 300),
      { initialProps: { v: 'a' } }
    );
    rerender({ v: 'b' });
    rerender({ v: 'c' });
    act(() => { jest.advanceTimersByTime(300); });
    expect(result.current).toBe('c');
  });

  it('clears its timer on unmount (no late setState)', () => {
    const { result, unmount } = renderHook(() => useDebouncedValue('x', 100));
    unmount();
    act(() => { jest.advanceTimersByTime(100); });
    expect(result.current).toBe('x');   // 没再被更新
  });
});
\`\`\`

跑下来三个 case 都过，关键点是：

- **fake timers** 让我们精确推进时间，不依赖 \`setTimeout\` 的真实延迟
- **act()** 包裹 timer 推进，保证 React 把这次更新 flush 出来再断言
- 卸载用例特意 **不** 用 \`rerender\`，直接 \`unmount()\`，验证 cleanup 函数跑了

要不要我再加一个用例，模拟组件 remount 后延迟重新生效？`,
    createdAt: now - 2 * 24 * hr + 12 * min
  },
  {
    id: 's3-m5',
    role: 'user',
    content: '够了，提交吧，CI 上跑一下回归。',
    createdAt: now - 2 * 24 * hr + 35 * min
  },
  {
    id: 's3-m6',
    role: 'assistant',
    content: `已提交 \`feat: useDebouncedValue unit tests\`，CI 全绿：

- ✅ Unit: 3 passed (jest)
- ✅ Lint: 0 warnings
- ✅ Coverage: hooks 100% lines / 100% branches`,
    createdAt: now - 2 * 24 * hr + 35 * min + 15_000
  }
];

/** s-4 · 缓存淘汰策略 */
const s4Messages: Message[] = [
  {
    id: 's4-m1',
    role: 'user',
    content: '我们这边订单服务的 Redis 命中率只有 61%，想系统地优化一下淘汰策略，给我一套方案。',
    createdAt: now - 4 * 24 * hr
  },
  {
    id: 's4-m2',
    role: 'assistant',
    content: '先把现在的 key 分布和命中率摸一下，再决定调 LRU 还是改 TTL。',
    createdAt: now - 4 * 24 * hr + 40_000,
    toolCalls: [
      {
        id: 's4-tc-redis',
        name: 'bash',
        summary: 'Bash · redis-cli --bigkeys',
        level: 'L1',
        status: 'success',
        args: { command: 'redis-cli -h prod-ord-1 --bigkeys' },
        result: `-------- summary -------
Biggest string found '"order:detail:88231"' has 9123 bytes
Most frequent prefix 'session:*' (312_881 keys, 41% of total)
Sampled 1_000_000 keys; 7% found in oldest 1% of keyspace`,
        startedAt: now - 4 * 24 * hr + 40_000,
        endedAt: now - 4 * 24 * hr + 70_000
      },
      {
        id: 's4-tc-hit',
        name: 'bash',
        summary: 'Bash · redis-cli INFO stats',
        level: 'L1',
        status: 'success',
        args: { command: 'redis-cli -h prod-ord-1 INFO stats | grep -E "hits|misses"' },
        result: `keyspace_hits: 8_412_991
keyspace_misses: 5_240_318   # 命中率 61.6%
expired_keys: 1_120_440
evicted_keys: 41_002`,
        startedAt: now - 4 * 24 * hr + 80_000,
        endedAt: now - 4 * 24 * hr + 82_000
      }
    ]
  },
  {
    id: 's4-m3',
    role: 'user',
    content: 'session:* 占 41% 是热点吗？会不会把订单的 key 挤掉？',
    createdAt: now - 4 * 24 * hr + 5 * min
  },
  {
    id: 's4-m4',
    role: 'assistant',
    content: `很可能是。**session:*** 体积小、数量多，LRU 淘汰时它会"幸存"，反而把订单详情这种大 key 挤出去。这就是你命中率上不去的根因。

## 推荐方案：分实例 + 分淘汰策略

| 实例 | 用途 | 数据特征 | maxmemory-policy |
| --- | --- | --- | --- |
| \`ord-cache-1\` | 订单详情、商品 SKU | 大 key、低 QPS | **allkeys-lfu** |
| \`ord-cache-2\` | session、临时 token | 小 key、高 QPS | **allkeys-lru** |
| \`ord-cache-3\` | 排行榜、计数器 | 长尾写多 | **volatile-ttl** |

具体落地：

1. **拆分**：按业务前缀把 key 分到三个 Redis 实例，互不干扰
2. **淘汰策略**：订单实例从 \`allkeys-lru\` 改成 \`allkeys-lfu\`，热点订单详情不会被冷数据挤掉
3. **TTL 分级**：订单详情 30 分钟、session 2 小时、排行榜 60 秒
4. **主动失效**：订单写完成功后，**必须**调用 \`DEL order:detail:<id>\`，不能等 TTL

预期效果：命中率从 61% 提到 85%+，evicted_keys 降到接近 0。要不要我先把 session 前缀的 key 抓个采样，估算迁移到独立实例后的内存占用？`,
    createdAt: now - 4 * 24 * hr + 25 * min
  },
  {
    id: 's4-m5',
    role: 'user',
    content: '先把采样脚本给我，我自己跑一下再说。',
    createdAt: now - 4 * 24 * hr + 2 * hr - 2 * min
  },
  {
    id: 's4-m6',
    role: 'assistant',
    content: `采样脚本 \`scripts/sample_session_keys.py\`，用 SCAN 不阻塞：

\`\`\`python
"""Sample 5% of session:* keys from prod-ord-1 and report memory usage."""
import redis, random, sys

SAMPLE_RATIO = 0.05
HOST = "prod-ord-1"

def main() -> int:
    r = redis.Redis(host=HOST, decode_responses=True)
    keys, total = [], 0
    for k in r.scan_iter(match="session:*", count=1000):
        total += 1
        if random.random() < SAMPLE_RATIO:
            keys.append(k)
    mem = sum(r.memory_usage(k) or 0 for k in keys)
    print(f"total session:* keys = {total}")
    print(f"sampled = {len(keys)} (≈{SAMPLE_RATIO*100:.0f}%)")
    print(f"estimated total memory = {mem / SAMPLE_RATIO / 1024 / 1024:.1f} MB")
    return 0

if __name__ == "__main__":
    sys.exit(main())
\`\`\`

跑法：\`python scripts/sample_session_keys.py\`，预计耗时 3-5 分钟（SCAN 是分批的）。把输出贴给我，我帮你算新实例的内存预算。`,
    createdAt: now - 4 * 24 * hr + 2 * hr
  }
];

/** s-5 · 翻译 gRPC 入门 */
const s5Messages: Message[] = [
  {
    id: 's5-m1',
    role: 'user',
    content: '把这篇 gRPC Quickstart 翻成中文，保留代码块，术语用国内常见译法。',
    createdAt: now - 6 * 24 * hr
  },
  {
    id: 's5-m2',
    role: 'assistant',
    content: '好，我先把原文抓下来看一遍结构。',
    createdAt: now - 6 * 24 * hr + 20_000,
    toolCalls: [
      {
        id: 's5-tc-fetch',
        name: 'web_fetch',
        summary: 'Fetch · grpc.io/docs/quickstart',
        level: 'L1',
        status: 'success',
        args: { url: 'https://grpc.io/docs/languages/python/quickstart' },
        result: 'Fetched 4_120 words, 3 code blocks, 1 architecture diagram',
        startedAt: now - 6 * 24 * hr + 20_000,
        endedAt: now - 6 * 24 * hr + 35_000
      }
    ]
  },
  {
    id: 's5-m3',
    role: 'user',
    content: '重点：把"service"、"stub"、"channel" 这几个词统一一下，文档里别一会儿翻译一会儿不翻译。',
    createdAt: now - 6 * 24 * hr + 2 * min
  },
  {
    id: 's5-m4',
    role: 'assistant',
    content: `下面是译文片段，开篇定义了一下术语表：

---

## 术语表

| 英文 | 译文 | 备注 |
| --- | --- | --- |
| service | 服务 | 不译，gRPC 文档里全程不译 |
| stub | 桩 / 客户端桩 | 首次出现给出英文原文，后续用"桩" |
| channel | 通道 | 不译 |
| message | 消息 | 不译 |
| RPC | RPC | 不译 |
| unary call | 一元调用 | |
| streaming call | 流式调用 | 包含 server / client / bidi 三类 |
| metadata | 元数据 | 不译 |
| interceptor | 拦截器 | |

## gRPC 快速上手（Python）

本指南用 \`grpcio\` 工具包演示一个最小可运行的 gRPC 服务。完成本指南后，你将拥有：

- 一个用 \`.proto\` 文件定义的服务
- 一段由 \`protoc\` 生成的 Python 桩代码
- 一个实现该服务的 server
- 一个调用该服务的 client

### 1. 定义服务

\`\`\`protobuf
syntax = "proto3";

package helloworld;

service Greeter {
  rpc SayHello (HelloRequest) returns (HelloReply) {}
}

message HelloRequest  { string name = 1; }
message HelloReply    { string message = 1; }
\`\`\`

> 注意：\`service Greeter\` 这一行**不译**。在 gRPC 生态里"服务"和"service"是同义词，但代码里的关键字必须保留英文。

### 2. 生成桩代码

\`\`\`bash
python -m grpc_tools.protoc \\
  --python_out=. --grpc_python_out=. \\
  -I . helloworld.proto
\`\`\`

生成两个文件：\`helloworld_pb2.py\`（消息类）和 \`helloworld_pb2_grpc.py\`（桩 + 服务基类）。

---

后面还剩两节（实现 server、实现 client）需要继续翻吗？`,
    createdAt: now - 6 * 24 * hr + 12 * min
  },
  {
    id: 's5-m5',
    role: 'user',
    content: '继续，把 server 和 client 都翻完，最后给我一份 Markdown。',
    createdAt: now - 5 * 24 * hr
  },
  {
    id: 's5-m6',
    role: 'assistant',
    content: `完整 Markdown 已经写好（约 1_200 字 + 5 个代码块），存在 \`docs/translated/grpc-quickstart-zh.md\`。要点：

- 全文统一用了上面那张术语表，**没有一处混用**
- 所有 \`protoc\` 命令、import 路径、文件名都没改动，避免读者复制粘贴出错
- 章节标题用了"1. / 2. / 3. ..."，和官方文档保持一致

需要我把这份文件提交到 \`docs/\` 目录下，并开一个 PR 吗？`,
    createdAt: now - 5 * 24 * hr + 30_000
  }
];

/** s-6 · SQL 索引优化 */
const s6Messages: Message[] = [
  {
    id: 's6-m1',
    role: 'user',
    content: '这条查询在生产上要跑 8 秒，看看能不能加个索引或者改写一下：\n\n```sql\nSELECT o.id, o.amount, u.email\nFROM orders o JOIN users u ON u.id = o.user_id\nWHERE o.created_at > now() - interval \'7 days\'\n  AND o.status = \'paid\'\nORDER BY o.amount DESC\nLIMIT 50;\n```',
    createdAt: now - 8 * 24 * hr
  },
  {
    id: 's6-m2',
    role: 'assistant',
    content: '先把执行计划拉出来，再决定是加索引还是改写。',
    createdAt: now - 8 * 24 * hr + 25_000,
    toolCalls: [
      {
        id: 's6-tc-explain',
        name: 'bash',
        summary: 'Bash · psql EXPLAIN ANALYZE',
        level: 'L1',
        status: 'success',
        args: { command: 'psql -c "EXPLAIN (ANALYZE, BUFFERS) SELECT ... LIMIT 50"' },
        result: `Limit  (cost=4218.00..4218.12 rows=50) (actual time=8123.41..8123.45 rows=50 loops=1)
  ->  Sort  (cost=4218.00..4234.16 rows=6462 width=...) (actual time=8123.40..8123.43 rows=50 loops=1)
        Sort Key: o.amount
        Sort Method: top-N heapsort  Memory: 35kB
        ->  Hash Join  (cost=1850.00..3940.18 rows=6462 width=...) (actual time=88.20..8118.50 rows=42_318 loops=1)
              Hash Cond: (o.user_id = u.id)
              ->  Seq Scan on orders o  (cost=0.00..1820.00 rows=6462 width=...) (actual time=0.04..8100.21 rows=42_318 loops=1)
                    Filter: ((created_at > ...) AND (status = 'paid'))
                    Rows Removed by Filter: 8_412_900
              ->  Hash  (cost=1200.00..1200.00 rows=52_000 width=...) (actual time=87.50..87.50 rows=52_000 loops=1)
Planning Time: 0.42 ms
Execution Time: 8123.45 ms`,
        startedAt: now - 8 * 24 * hr + 25_000,
        endedAt: now - 8 * 24 * hr + 27_000
      }
    ]
  },
  {
    id: 's6-m3',
    role: 'user',
    content: '那 840 万行 seq scan 太夸张了，加个组合索引够不够？',
    createdAt: now - 8 * 24 * hr + 3 * min
  },
  {
    id: 's6-m4',
    role: 'assistant',
    content: `够。问题是 **过滤后再排序**，所以索引列的顺序必须是 \`(status, created_at, amount DESC)\`。

## 建议索引

\`\`\`sql
CREATE INDEX CONCURRENTLY idx_orders_status_created_amount
  ON orders (status, created_at DESC, amount DESC)
  WHERE status = 'paid';   -- 部分索引，命中子集
\`\`\`

要点：
- **status 在第一列**：因为 WHERE 里它是等值过滤
- **created_at 第二列 + DESC**：和 ORDER BY 方向一致
- **amount 第三列 + DESC**：可以直接走 index scan + top-N，**不再需要 Sort**
- **WHERE 子句**：只索引已支付订单，写入不会被未支付订单拖慢

## 改写后的预期执行计划

\`\`\`
Limit  (actual time=0.18..0.42 rows=50)
  ->  Index Scan using idx_orders_status_created_amount on orders o
        Index Cond: (status = 'paid' AND created_at > ...)
        ->  Index Lookup on users u
\`\`\`

## 备用方案

如果业务上"近 7 天已支付"已经能覆盖 95% 的请求，可以再加一个 \`created_at >= now() - interval '14 days'\` 的物化视图，每天凌晨刷新一次，把 LIMIT 50 的查询打到视图上。

预计效果：

| 改动 | 当前 | 改后 |
| --- | --- | --- |
| 耗时 | 8.1 s | 30 - 80 ms |
| 扫描行 | 8.4 M | 4 - 8 K |

要不要我先把这条索引创建脚本写成迁移文件？`,
    createdAt: now - 8 * 24 * hr + 12 * min
  },
  {
    id: 's6-m5',
    role: 'user',
    content: '出迁移文件，我让 DBA 在低峰期跑。',
    createdAt: now - 8 * 24 * hr + 15 * min
  },
  {
    id: 's6-m6',
    role: 'assistant',
    content: `迁移文件 \`migrations/2026_09_14_orders_idx.sql\`：

\`\`\`sql
-- UP
CREATE INDEX CONCURRENTLY idx_orders_status_created_amount
  ON orders (status, created_at DESC, amount DESC)
  WHERE status = 'paid';

-- DOWN
DROP INDEX IF EXISTS idx_orders_status_created_amount;
\`\`\`

注意三件事：

1. **CONCURRENTLY**：不锁表，但会比普通 CREATE INDEX 慢，估计 4-6 分钟
2. **WHERE status = 'paid'**：部分索引，必须在迁移里写明，否则建好之后 drop 不到原索引
3. **顺序**：必须先建唯一索引迁移（前面那条 users.email），再跑这条，否则 \`users\` 表 join 会回退到 hash join

回滚脚本里 DROP INDEX 用 IF EXISTS，避免重复回滚时报错。`,
    createdAt: now - 8 * 24 * hr + 15 * min + 12_000
  }
];

/** Map session id -> message history, used by the store on first hydration. */
export const mockMessagesBySession: Record<string, Message[]> = {
  's-1': mockLiveMessages,
  's-2': s2Messages,
  's-3': s3Messages,
  's-4': s4Messages,
  's-5': s5Messages,
  's-6': s6Messages
};

export const mockModels: ModelConfig[] = [
  {
    id: 'm-deepseek',
    name: 'DeepSeek · 主用',
    baseUrl: 'https://api.deepseek.com/v1',
    apiKey: 'sk-ds...2f',
    model: 'deepseek-chat',
    inputPrice: 0.001,
    outputPrice: 0.002,
    isDefault: true
  },
  {
    id: 'm-claude',
    name: 'Claude 3.7 · 长文',
    baseUrl: 'https://api.anthropic.com/v1',
    apiKey: 'sk-an...1e',
    model: 'claude-3-7-sonnet',
    inputPrice: 0.018,
    outputPrice: 0.054
  },
  {
    id: 'm-gpt4o',
    name: 'GPT-4o · 代码',
    baseUrl: 'https://api.openai.com/v1',
    apiKey: 'sk-**...b9',
    model: 'gpt-4o',
    inputPrice: 0.015,
    outputPrice: 0.045
  },
  {
    id: 'm-qwen',
    name: '通义千问 · 备用',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    apiKey: 'sk-**...40',
    model: 'qwen-plus',
    inputPrice: 0.004,
    outputPrice: 0.012
  }
];

export const mockPermissionRules: PermissionRule[] = [
  { id: 'r-1', pattern: 'Bash: ls *', action: 'allow', description: '列目录只读，安全' },
  { id: 'r-2', pattern: 'Bash: cat *', action: 'allow', description: '读文件内容，安全' },
  { id: 'r-3', pattern: 'Bash: pytest *', action: 'allow', description: '跑测试，安全' },
  { id: 'r-4', pattern: 'Bash: pip install *', action: 'ask', description: '装包，要看一眼版本' },
  { id: 'r-5', pattern: 'Bash: rm *', action: 'ask', description: '删除操作，必须确认' },
  // 2026-09-24：L3 不是「禁止」而是「每次都要人工确认」，且不可记住 —— 文案别写成硬拦
  { id: 'r-6', pattern: 'Write: /etc/**', action: 'ask', description: '系统目录写入：每次都要人工确认' },
  { id: 'r-7', pattern: 'Write: ~/.ssh/**', action: 'ask', description: 'SSH 密钥目录写入：每次都要人工确认' },
];