"""
Behavior Fix Plugin — 修复 AI 行为问题。

三个功能：
1. Time Anchor   — 在 prompt 最前面注入当前北京时间
2. Repeat Blocker — 拦截 bot 连续发送的重复内容
3. Correction Booster — 检测用户纠正，强制 LLM 承认错误并修正
"""

import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


# ---------------------------------------------------------------------------
# Beijing time helper (inlined so we have zero cross-plugin dependencies)
# ---------------------------------------------------------------------------

def _get_beijing_time_str() -> str:
    """返回当前北京时间字符串，格式: YYYY-MM-DD HH:MM:SS (周X)"""
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({weekdays[now.weekday()]})"


# ---------------------------------------------------------------------------
# Default correction trigger patterns
# ---------------------------------------------------------------------------

DEFAULT_CORRECTION_PATTERNS = [
    "不对", "你错了", "纠正", "不是这样的",
    "你理解错了", "搞错了", "说错了", "别瞎说",
]

CORRECTION_DIRECTIVE = (
    '[系统指令] 用户刚才指出你说错了。你必须：'
    '1) 先承认错误，不要狡辩；'
    '2) 放弃之前错误的假设；'
    '3) 根据用户的纠正重新回答。'
    '不要说”你说得对”这种敷衍的话，要具体说明你错在哪里。'
)


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

@register(
    "astrbot_plugin_behavior_fix",
    "BaiEmu",
    "修复 AI 行为问题：时间感知、重复拦截、纠正增强",
    "1.0.0",
    "",
)
class BehaviorFixPlugin(Star):
    """行为修复插件主类。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.logger = logger

        cfg = config or {}

        # --- Feature toggles ---
        ta = cfg.get("time_anchor", {})
        self._ta_enabled = ta.get("enabled", True) if isinstance(ta, dict) else True

        rb = cfg.get("repeat_blocker", {})
        self._rb_enabled = rb.get("enabled", True) if isinstance(rb, dict) else True
        self._rb_window_size: int = rb.get("window_size", 5) if isinstance(rb, dict) else 5
        self._rb_window_seconds: int = rb.get("window_seconds", 300) if isinstance(rb, dict) else 300

        cb = cfg.get("correction_booster", {})
        self._cb_enabled = cb.get("enabled", True) if isinstance(cb, dict) else True
        self._cb_patterns: List[str] = (
            cb.get("patterns", DEFAULT_CORRECTION_PATTERNS)
            if isinstance(cb, dict) else DEFAULT_CORRECTION_PATTERNS
        )

        # --- Repeat blocker state ---
        # key (group_id or user_id) -> deque of (timestamp, text_stripped)
        self._recent_outputs: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self._rb_window_size)
        )

        self.logger.info(
            f"[BehaviorFix] Loaded: time_anchor={self._ta_enabled}, "
            f"repeat_blocker={self._rb_enabled}, correction_booster={self._cb_enabled}"
        )

    # ==================================================================
    # Hook: on_llm_request (priority=5 — before memory at 40)
    # ==================================================================

    @filter.on_llm_request(priority=5)
    async def on_llm_request_behavior_fix(
        self, event: AstrMessageEvent, request: ProviderRequest
    ):
        """Inject time anchor and/or correction directive into the prompt."""

        # 1) Time anchor
        if self._ta_enabled:
            time_line = (
                f"[系统时间] 现在是北京时间 {_get_beijing_time_str()}。"
                "请根据此时间判断对话中事件的发生时间。"
            )
            if request.system_prompt:
                request.system_prompt = time_line + "\n\n" + request.system_prompt
            else:
                request.system_prompt = time_line

        # 2) Correction booster
        if self._cb_enabled:
            user_text = event.get_message_outline()
            if user_text and self._has_correction_pattern(user_text):
                if request.system_prompt:
                    request.system_prompt += "\n\n" + CORRECTION_DIRECTIVE
                else:
                    request.system_prompt = CORRECTION_DIRECTIVE
                self.logger.info(
                    f"[BehaviorFix] Correction directive injected "
                    f"(message preview: {user_text[:80]})"
                )

    # ==================================================================
    # Hook: on_decorating_result (priority=10 — before output pipeline at 50)
    # ==================================================================

    @filter.on_decorating_result(priority=10)
    async def on_decorating_result_behavior_fix(self, event: AstrMessageEvent):
        """Block repeated bot outputs before they are sent."""
        if not self._rb_enabled:
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        plain = result.get_plain_text()
        if not plain or not plain.strip():
            return

        stripped = plain.strip()
        session_key = self._session_key(event)
        now = time.time()

        # Purge expired entries for this session
        self._purge_expired(session_key, now)

        # Check for duplicate
        if self._is_duplicate(session_key, stripped):
            self.logger.info(
                f"[BehaviorFix] Blocked repeat output in {session_key}: "
                f"'{stripped[:100]}'"
            )
            event.stop_event()
            return

        # Record this output
        self._recent_outputs[session_key].append((now, stripped))

    # ==================================================================
    # Helpers
    # ==================================================================

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        """Return a stable session key: group_id for groups, sender_id for private."""
        gid = event.get_group_id()
        return gid if gid else event.get_sender_id()

    def _has_correction_pattern(self, text: str) -> bool:
        """Check if the user message contains any correction trigger word."""
        for pat in self._cb_patterns:
            if pat in text:
                return True
        return False

    def _is_duplicate(self, session_key: str, text: str) -> bool:
        """Check if text exactly matches any recent output in the window."""
        for _ts, prev in self._recent_outputs[session_key]:
            if prev == text:
                return True
        return False

    def _purge_expired(self, session_key: str, now: float):
        """Remove entries older than _rb_window_seconds."""
        cutoff = now - self._rb_window_seconds
        dq = self._recent_outputs[session_key]
        while dq and dq[0][0] < cutoff:
            dq.popleft()
