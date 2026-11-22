# -*- coding: utf-8 -*-
"""数据源重试与 daily_job 退出码语义回归（2026-11-20/21 CI 失败根因）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import HTTP_RETRIES  # noqa: E402
from fetch.sources import _retry_call  # noqa: E402
from scripts.daily_job import _should_fail_fetch  # noqa: E402


class TestRetryCall:
    def test_transient_failure_then_success(self):
        """前 N-1 次失败、第 N 次成功 → 返回成功值。"""
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < HTTP_RETRIES:
                raise ConnectionError("simulated transient")
            return "ok"

        assert _retry_call(flaky) == "ok"
        assert len(calls) == HTTP_RETRIES

    def test_all_failed_returns_none(self):
        """全次失败 → 返回 None（优雅降级，不抛异常到上层）。"""
        calls = []

        def always_fail():
            calls.append(1)
            raise TimeoutError("simulated persistent")

        assert _retry_call(always_fail) is None
        assert len(calls) == HTTP_RETRIES


class TestDailyJobExitCode:
    def test_all_sources_failed_returns_true(self):
        assert _should_fail_fetch({"klines": 0, "share_rows": 0, "margin_rows": 0}) is True

    def test_shares_only_empty_still_returns_false(self):
        """份额单点失败（K线正常）不再阻断部署——本次修复的核心语义。"""
        assert _should_fail_fetch({"klines": 28, "share_rows": 0, "margin_rows": 0}) is False

    def test_margin_only_empty_still_returns_false(self):
        assert _should_fail_fetch({"klines": 28, "share_rows": 65, "margin_rows": 0}) is False

    def test_all_ok_returns_false(self):
        assert _should_fail_fetch({"klines": 28, "share_rows": 65, "margin_rows": 22}) is False
