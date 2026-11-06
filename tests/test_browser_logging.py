"""Regression checks for browser error logging and Streamlit control flow."""

from __future__ import annotations

import logging

import pytest

import review_app
from src.logging_config import setup_logger
from src.ui import browser_app


def _close_logger(logger: logging.Logger) -> None:
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()


def test_browser_main_logs_full_redacted_traceback_and_reraises(monkeypatch, tmp_path):
    log_path = tmp_path / "logs" / "browser.log"
    logger = setup_logger("test.browser.failure", str(log_path))
    monkeypatch.setattr(review_app, "logger", logger)

    def fail_render():
        raise RuntimeError("request failed: Bearer very-secret-token sk-proj-12345678901234567890")

    monkeypatch.setattr(browser_app, "render_browser_app", fail_render)
    try:
        with pytest.raises(RuntimeError, match="request failed"):
            review_app.main()

        logged = log_path.read_text(encoding="utf-8")
        assert "Unexpected browser workflow failure" in logged
        assert "Traceback (most recent call last)" in logged
        assert "fail_render" in logged
        assert "RuntimeError:" in logged
        assert "Bearer [REDACTED]" in logged
        assert "[REDACTED_API_KEY]" in logged
        assert "very-secret-token" not in logged
        assert "sk-proj-12345678901234567890" not in logged
    finally:
        _close_logger(logger)


def test_browser_main_does_not_log_streamlit_baseexception_control_flow(monkeypatch, tmp_path):
    class StreamlitControlFlow(BaseException):
        pass

    log_path = tmp_path / "logs" / "control-flow.log"
    logger = setup_logger("test.browser.control_flow", str(log_path))
    monkeypatch.setattr(review_app, "logger", logger)

    def rerun():
        raise StreamlitControlFlow("rerun")

    monkeypatch.setattr(browser_app, "render_browser_app", rerun)
    try:
        with pytest.raises(StreamlitControlFlow):
            review_app.main()
        assert "Unexpected browser workflow failure" not in log_path.read_text(encoding="utf-8")
    finally:
        _close_logger(logger)
