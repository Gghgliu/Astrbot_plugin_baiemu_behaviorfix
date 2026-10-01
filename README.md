# astrbot_plugin_behavior_fix — bot行为增强插件
纠正时间，群聊重复发文，纠正信息量

##本插件包含AI生成代码，请谨慎使用

## 插件功能

3个核心行为：
1. **增强时间**
2. **群聊错误**
3. **纠正无效**


## 功能设计

### 1. Time Anchor（时间锚点）

**Hook**: `@filter.on_llm_request(priority=5)`
**行为**:
- 在 `event.request.system_prompt` 最前面注入一行：
  ```
  [系统时间] 现在是北京时间 2026-05-05 14:30:00 (周一)。请根据此时间判断对话中事件的发生时间。
  ```- `get_beijing_time_str()` 内联实现（从 angel_heart/time_utils.py 复制，约 15 行）
**配置**: `time_anchor.enabled` (bool, default true)

### 2. Repeat Blocker（重复拦截）
**Hook**: `@filter.on_decorating_result(priority=10)`
**行为**:
- 维护每个 group_id/private 的最近 N 条 bot 输出指纹（SHA256 或 text hash）
- 当前 bot 输出与窗口内任一输出相同 → 调用 `event.stop_event()` 拦截
- 同时维护一个简单的文本精确匹配窗口（比 hash 更快，先做精确匹配再 fallback hash）
- 拦截时记录日志：`[BehaviorFix] Blocked repeat in group=xxx`
**配置**:
- `repeat_blocker.enabled` (bool, default true)
- `repeat_blocker.window_size` (int, default 5, hint="保留最近 N 条 bot 输出用于比对")
- `repeat_blocker.window_seconds` (int, default 300, hint="时间窗口秒数，超时的旧记录自动过期")
**现有 check_repeat**: 现有实现只检测不拦截，这个插件**实际调用 `event.stop_event()` 阻止发送**。

### 3. Correction Booster（纠正增强）
**Hook**: `@filter.on_llm_request(priority=5)`（与 time anchor 同一 hook）
**行为**:
- 检测用户消息中是否包含纠正模式：`不对`、`你错了`、`纠正`、`不是这样的`、`你理解错了`、`搞错了`、`说错了`、`别瞎说`
- 检测到纠正时，在 system_prompt 末尾注入强指令：
  ```
  [系统指令] 用户刚才指出你说错了。你必须：1) 先承认错误，不要狡辩；2) 放弃之前错误的假设；3) 根据用户的纠正重新回答。不要说"你说得对"这种敷衍的话，要具体说明你错在哪里。
  ```
  - 考虑上下文：只检测当前轮用户消息（`event.request.user_message`），不检测历史
**配置**:
- `correction_booster.enabled` (bool, default true)
- `correction_booster.patterns` (list of string, default 8 个中文纠正短语)

同Astrbot插件，未经过安全连通性测试，谨慎使用，且构建时间过久现已废弃。
