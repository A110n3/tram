# 翻译链路性能优化分析

## Context

用户要求对翻译全链路做性能审查，找出所有可优化的点（哪怕是小优化），为后续的"同步翻译"功能做准备。同步翻译通常意味着更高的触发频率、更低的延迟要求、更频繁的取消与重启，因此当前设计中"每次请求都从零开始"的模式可能成为瓶颈。

本文档是分析报告，列出所有优化机会，按收益/成本排序，供后续分期实施。

---

## 翻译链路全景

```
热键触发 → 取词/截图&识别 → 去重缓存 → 分段 → 提示词构造 → 后端请求 → SSE解析 → UI流式渲染
   │           │              │         │        │              │          │         │
GlobalHotkey  grab_selection  _last_text split_text build_messages chat_stream json.loads append_token
              OCRWorker       缓存       chunking  prompts.py   httpx      逐行解析    setText
              (RapidOCR)                                                        heightForWidth
```

---

## 优化项清单（按优先级排序）

### P0 — 高收益 / 低成本，同步翻译刚需

#### 1. Token 批量合并 + UI 节流

**位置**: `app/ui/popup.py` → `append_token()` + `app/ui/worker.py` → `TranslateWorker.run()`

**问题**: 每个 token 到来都触发一次完整的 UI 更新：`setText()` → `_adjust_height()`（含 `heightForWidth` 布局计算）→ `_keep_on_screen()` → `QTimer.singleShot(0, _scroll_to_bottom)`。流式翻译每秒 20-50 个 token 时，UI 线程被大量重绘占用。

**优化方案**:
- 在 popup 侧加一个 30-50ms 的合并定时器：攒够一批 token 一次性 `setText` + 调高度
- 高度计算进一步节流到每 100ms 一次（滚动条跟随同样节流）
- 翻译完成时 flush 剩余 buffer，确保终态文本无延迟

**预期收益**: UI 线程负载降低 60-80%，流式更新更平滑，同步翻译高频更新时不卡。

**改动量**: 小（popup.py 增加 ~40 行，worker 侧不动）

---

#### 2. 翻译提示词缓存

**位置**: `app/core/translator.py` → `Translator.translate()` + `app/core/prompts.py` → `build_messages()`

**问题**: 每次翻译都重新构造系统提示词：
- `to_prompt_block(config.get("glossary", []))` — 遍历术语表拼字符串
- `build_default_system_prompt()` — 多次 dict lookup + 字符串 format
- 多块翻译时，每块都重新调一次 `build_messages()`（虽然每块的 context_block 不同，但 system 部分除了 context 是一样的）

**优化方案**:
- 在 Translator 实例内缓存系统提示词骨架：以 `(target_lang, source_lang, style, glossary_tuple, custom_prompt, use_system_role)` 为 key，缓存渲染好的 system prompt
- `translate()` 开始时计算一次 key，命中则直接复用
- 多块翻译时，只把变化的 context_block 拼进去，system 主体不复用拼接

**预期收益**: 每次翻译省掉 ~10 次字符串操作 + 术语表遍历。单次翻译收益小，但同步翻译高频触发下累计可观。代码复杂度增加不多。

**改动量**: 小（translator.py 增加 ~20 行缓存逻辑）

---

#### 3. 取消响应速度审查 & 优化

**位置**: `app/core/backend.py` → `cancel()` + `app/ui/worker.py` → `TranslateWorker.request_stop()`

**问题**: 同步翻译场景下取消会非常频繁（用户打字时每输入一个字可能就取消上一次）。当前取消路径是：
1. `request_stop()` 设 `_stop_flag` + 调 `backend.cancel()`
2. `backend.cancel()` 设置 `_cancel_event` + 关闭 `_current_response` + 关闭整个 `_client`
3. 关闭 client 意味着下次请求需要重建 TCP 连接 + TLS 握手（本地后端无 TLS 但 TCP 握手仍在）

**优化方案**:
- **不关闭整个 client**：`_current_response.close()` 已经能打断正在读取的流；对于阻塞在"连接建立阶段"的请求，改用 `_cancel_event` + 超时控制来处理。保留连接池，避免每次取消后重建连接。
- 验证：当前 `cancel()` 关整个 client 的原因是"打断阻塞在连接建立或首字节等待的请求"——但对于本地后端（localhost），连接建立几乎是瞬时的，首字节等待才是慢的部分，而首字节等待时 `_current_response` 已经存在了（`send()` 返回后 response 对象就有了）。需要确认 httpx 的 `stream=True` 下 `send()` 是否阻塞到首字节。
  - 若 `send()` 阻塞到响应头到达：那 `_current_response` 在首字节后才可用，首字节前的阻塞只能靠关 client
  - 本地后端场景下连接建立快，主要等待是模型推理（首字节延迟），此时 response 已经存在，关 response 就够了
- **结论**：可以保留"关 client"作为兜底，但加一个快速路径：先关 response，response 不存在时才关 client

**预期收益**: 取消后立即发起新请求时，复用已有连接，省掉 TCP 握手（~1ms 本地可忽略，但对远程后端有意义）。主要价值是减少连接池重建的开销和潜在的 TIME_WAIT 堆积。

**改动量**: 中（需要仔细测试取消的各种时机：连接中/首字节等待/流式读取中）

---

### P1 — 中收益 / 中成本

#### 4. 去重缓存扩展为 LRU

**位置**: `app/ui/base_translator.py` → `_last_text` / `_last_result`

**问题**: 当前只缓存最近 1 条翻译结果。用户反复查看不同段落时（同步翻译场景下光标在段落间跳动），每次切回旧段落都要重新翻译。

**优化方案**:
- 改成 LRU 缓存，容量 10-20 条
- key 用 `(text, target_lang, source_lang)` 的哈希，value 是译文
- 配置变更（术语表/风格/自定义提示词）时整个缓存清空
- 仍保留 `_last_text` / `_last_result` 作为最近一条的快捷引用（兼容重试逻辑）

**预期收益**: 反复查看同几段文本时命中率大幅提升。同步翻译场景下如果用户在固定区域内滚动/切换，缓存命中会很高。

**改动量**: 小（新增 ~50 行 LRU 实现，或直接用 `functools.lru_cache` 包装一个函数）

**注意**: 内存开销可忽略（10 段文本也就几十 KB），但需要考虑缓存失效策略——配置变更时要正确清空。

---

#### 5. SSE 解析微优化

**位置**: `app/core/backend.py` → `chat_stream()`

**问题**: 每一行 SSE 数据都做：
- `line.startswith("data:")` 字符串比较
- `json.loads(data)` 完整 JSON 解析
- 多级 dict 索引 `obj["choices"][0].get("delta", {}).get("content", "")`

**优化方案**:
- 用 `line[5:]` 替代 `line[len("data:"):].strip()`（已知前缀长度，省一次 strip 调用）—— 收益极小
- 对于 `json.loads`，可以用 `orjson` 替代标准库 json（如果依赖可接受），快 2-5 倍
- 手动解析 `content` 字段的 JSON 子集（从 `"content":"` 后截取到下一个 `"`），跳过完整 JSON 解析 —— 但不同后端的 JSON 字段顺序可能不同，健壮性下降

**预期收益**: 标准库 json 已经用 C 实现，每秒几十次解析不是瓶颈。换 orjson 可能省几毫秒，但增加了一个 C 扩展依赖。

**改动量**: 极小（换 orjson 只需改 import 和调用）到 中（手动解析则风险较高）

**建议**: 暂不做，除非 profiling 确认 JSON 解析是热点。

---

#### 6. OCR 图像预处理优化

**位置**: `app/core/ocr.py` → `crop_and_upscale()`

**问题**: OCR 前的图像放大用 `cv2.INTER_CUBic`，质量好但较慢。对于小文本区域放大 2-3 倍，这可能占 OCR 总耗时的相当比例。

**优化方案**:
- 评估 `cv2.INTER_LINEAR`（双线性） vs `INTER_CUBIC` 的识别率差异
- 如果差异不大，换 INTER_LINEAR，速度提升约 30-50%
- 或者更激进：用 `cv2.INTER_NEAREST` 放大，然后做一次轻度模糊抗锯齿

**预期收益**: 小选区 OCR 速度提升 10-30%。但需要实验验证识别率是否下降。

**改动量**: 极小（改一个参数）

---

### P2 — 架构级优化，为同步翻译量身定做

#### 7. 增量翻译（差分更新）

**位置**: `app/core/translator.py` 新增逻辑

**问题**: 同步翻译场景下，用户输入/选中的文本是逐字变化的。每次变化都重新翻译整段，浪费算力和带宽。

**优化方案**:
- 检测文本变化量：如果只是末尾追加了几个字（增量 < 20%），且上一块翻译还在进行中，可以：
  - 不取消当前请求，等它完成
  - 对新增的后缀单独发起一次翻译请求
  - 最后把两段译文拼起来
- 或者更简单：使用"滚动翻译"策略——维护一个已翻译的前缀和待翻译的后缀，每次只翻译后缀部分

**预期收益**: 同步翻译场景下带宽和延迟大幅降低。但实现复杂度高，需要处理很多边界情况（用户删改中间内容、翻译风格一致性、术语统一等）。

**改动量**: 大（需要重新设计翻译管线的状态管理）

---

#### 8. 持久连接 + 并行请求

**位置**: `app/core/backend.py`

**问题**: 当前是同步阻塞式请求（一个 backend 实例同一时间只处理一个请求）。如果未来需要同时处理多个翻译流（如划词 + OCR 同时进行），需要两个 backend 实例（当前设计就是如此，SelectionTranslator 和 OCRTranslator 各有自己的 backend）。

**优化方案**:
- 引入连接池 + 请求队列，支持并发多个请求
- 或者保持当前单请求设计，但明确"一个翻译器一个 backend"的模式是有意为之的设计决策

**预期收益**: 对于"划词和 OCR 同时翻译"的场景（目前很少见），可以避免等待。但实际使用中几乎不会同时触发，优先级低。

**改动量**: 大

---

#### 9. 预连接 & 模型预热

**位置**: `app/ui/main_window.py` → `_start_warmup()` + 空闲时触发

**问题**: 当前只有启动时做一次预热。长时间闲置后（比如几十分钟），后端可能卸载模型，下次翻译需要重新加载，首字节延迟可达数秒到数十秒。

**优化方案**:
- 增加空闲预热：检测到用户一段时间没有操作后，悄悄发一个最小请求保活
- 或者在用户鼠标悬停在文本上时（如果能检测到）就开始预连接
- 更简单：热键按下的瞬间（取词/框选进行中）就向后端发起一个空的预连接请求，等到真正要翻译时 TCP 连接已经建好了

**预期收益**: 首次翻译/冷启动翻译的"首字节延迟"显著降低。用户感知是"按完热键译文就出来了"而不是"等半天"。

**改动量**: 中（需要设计预热策略和时机）

---

### P3 — 微优化，累计效应

#### 10. 配置读取本地化

**位置**: `app/core/translator.py` → `translate()` 和 `_translate_chunk()`

**问题**: 每块翻译都从 `self.config.get("translation", {})` 再 `.get("target_lang", get_default(...))` 链式读取。虽然 dict 查找很快，但嵌套 + 默认值回退也有几次函数调用。

**优化方案**:
- `translate()` 开头一次性把所有配置读出来存为局部变量
- 后续循环直接用局部变量

**预期收益**: 极小（微秒级），但代码清晰性反而提升（配置集中在函数开头）。

**改动量**: 极小

---

#### 11. 字符串拼接优化

**位置**: `app/ui/popup.py` → `append_token()` 中的 `cur + token`

**问题**: 每次 append 都做 `setText(cur + token)`，其中 `cur = self._target_label.text()` 需要从 QLabel 取回完整文本。QLabel 的 text() 返回是深拷贝吗？Qt 中是隐式共享的，但 Python 包装层可能会做一次复制。

**优化方案**:
- popup 侧维护一个 `_current_text: str` 的 Python 端缓存
- `append_token()` 直接 `self._current_text += token` 然后 `setText(self._current_text)`
- 省去从 QLabel 读回文本的开销（虽然不大）
- 更重要的是：`setText` 会触发 QLabel 内部重新布局，如果能批量更新（见 P0-1），收益更大

**预期收益**: 单独做收益很小，配合 token 批量合并一起做。

**改动量**: 极小

---

#### 12. 滚动条跟随优化

**位置**: `app/ui/popup.py` → `append_token()` 中的 `QTimer.singleShot(0, self._scroll_to_bottom)`

**问题**: 每个 token 都 post 一个 0ms timer 事件来滚动到底部。Qt 的事件循环中，这些 timer 事件会和重绘事件交替排队。

**优化方案**:
- 配合 token 批量合并，滚动也批量做
- 或者用 `QMetaObject.invokeMethod` + `QueuedConnection` 替代（差异不大）

**预期收益**: 配合 token 合并一起收益明显，单独做可忽略。

**改动量**: 极小

---

## 建议实施顺序

### 第一阶段（同步翻译基础）
1. **Token 批量合并 + UI 节流** — 解决高频更新时的 UI 卡顿
2. **提示词缓存** — 减少重复构造开销
3. **取消路径优化** — 频繁取消不破坏连接池

### 第二阶段（体验优化）
4. **LRU 去重缓存** — 反复查看同一段文本时秒出
5. **空闲预热 / 预连接** — 降低首字节延迟感知
6. **配置读取本地化** — 顺手清理，顺带微优化

### 第三阶段（架构升级，视需求而定）
7. **增量翻译** — 同步翻译核心能力
8. **持久连接 & 并发支持** — 多流翻译场景

---

## 验证方式

- 所有改动必须通过现有 131 个测试（`pytest tests/`）
- 性能基准：写一个简单脚本，用 mock backend 模拟 100 token/s 的流式输出，测量 UI 帧率 / CPU 占用
- 取消延迟测试：测量从调用 `cancel()` 到 `run()` 返回的时间，确保 < 50ms
- `ruff check` + `mypy` 通过
