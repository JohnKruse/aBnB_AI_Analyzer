"""Bounded, cancellable retries and shared pacing for unattended collection."""
from __future__ import annotations

import re
import threading
import time
from urllib.error import HTTPError
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import requests

_LOCK = threading.Lock()
_NEXT_REQUEST: dict[str, float] = {}


class RunCancelled(RuntimeError):
    pass


def automatic_options(values=None):
    values = values or {}
    years = int(values.get('review_years', 2))
    cap = int(values.get('max_reviews', 25))
    attempts = int(values.get('max_attempts', 3))
    rounds = int(values.get('max_resume_rounds', 2))
    if not 0 <= rounds <= 2:
        raise ValueError('Choose 0–2 automatic resume rounds.')
    if years not in (0, 1, 2) or not 1 <= cap <= 500 or not 1 <= attempts <= 5:
        raise ValueError('Choose 1–2 years (or all dates), 1–500 reviews, and 1–5 attempts.')
    today = datetime.now(timezone.utc).date()
    try:
        cutoff = today.replace(year=today.year-years).isoformat() if years else None
    except ValueError:
        cutoff = today.replace(year=today.year-years, day=28).isoformat()
    cutoff = values.get('review_cutoff', cutoff)
    if cutoff is not None:
        from datetime import date
        cutoff = date.fromisoformat(str(cutoff)).isoformat()
    return {'review_years': years, 'review_cutoff': cutoff,
            'max_reviews': cap, 'max_attempts': attempts, 'max_resume_rounds': rounds}


def http_status(exc):
    response = getattr(exc, 'response', None)
    for obj, names in ((response, ('status_code', 'status')), (exc, ('status_code', 'status', 'code'))):
        for name in names:
            try:
                code = int(getattr(obj, name, None))
                if 100 <= code <= 599:
                    return code
            except (ValueError, TypeError):
                pass
    match = re.search(r'\bHTTP(?: Error)?\s+(\d{3})\b', str(exc)[:1000], re.I)
    return int(match.group(1)) if match else None


def retryable(exc):
    response = getattr(exc, 'response', None)
    code = http_status(exc)
    if code == 429:
        try:
            error = response.json().get('error', {})
            if isinstance(error, dict) and error.get('code') in {'insufficient_quota', 'billing_hard_limit_reached'}:
                return False
        except (ValueError, AttributeError):
            pass
    return code in {408, 429, 500, 502, 503, 504} or isinstance(exc, (requests.Timeout, requests.ConnectionError))


def retry_delay(exc, attempt):
    response = getattr(exc, 'response', None)
    headers = getattr(response, 'headers', None) or getattr(exc, 'headers', {}) or {}
    raw = headers.get('Retry-After')
    if raw:
        try:
            return max(0., float(raw))
        except ValueError:
            try:
                return max(0., (parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
    code = http_status(exc)
    return min(300., (60. if code == 429 else 2.) * 2**(attempt-1))


class RequestPolicy:
    def __init__(self, options, should_cancel=None, progress=None, property_count=1):
        self.options = options
        self.should_cancel = should_cancel or (lambda: False)
        self.progress = progress
        self.retry_budget_seconds = min(3600, max(1200, int(property_count)*60))
        self.retry_waited_seconds = 0.
        self.retry_stop_reason = None
        self.slowdown = {}
        self.wait_state = None
        self._retry_wait = False

    def wait_retry(self, seconds, message):
        if seconds > self.retry_budget_seconds-self.retry_waited_seconds:
            self.retry_stop_reason = 'Automatic retry waiting budget reached; checkpoints retained.'
            return False
        self.retry_waited_seconds += seconds
        self._retry_wait = True
        try:
            self.wait(seconds, message)
        finally:
            self._retry_wait = False
        return True

    def cooldown_remaining(self, service='airbnb'):
        with _LOCK:
            return max(0., _NEXT_REQUEST.get(service, 0)-time.monotonic())


    def wait(self, seconds, message):
        deadline = time.monotonic()+seconds
        self.wait_state = {
            'until': time.time()+seconds, 'seconds': seconds,
            'kind': 'retry' if self._retry_wait else 'pacing', 'message': message,
        } if seconds > 0 else None
        announced = False
        try:
            while True:
                if self.should_cancel():
                    raise RunCancelled('Run cancelled; checkpoints retained.')
                left = deadline-time.monotonic()
                if left <= 0:
                    return
                if self.progress:
                    announced = True
                    self.progress(message + f' ({int(left)+1}s remaining)')
                threading.Event().wait(min(1., left))
        finally:
            self.wait_state = None
            if announced and self.progress:
                self.progress('Wait finished; resuming work.' if not self.should_cancel()
                              else 'Run cancelled; checkpoints retained.')

    def call(self, function: Callable, *, service='airbnb', interval=None, operation=None, max_attempts=None):
        if interval is None:
            interval = 3. if service == 'airbnb' else 0.5
        attempts = self.options['max_attempts'] if max_attempts is None else max(1, int(max_attempts))
        for attempt in range(1, attempts+1):
            if self.should_cancel():
                raise RunCancelled('Run cancelled; checkpoints retained.')
            with _LOCK:
                now = time.monotonic()
                slot = max(now, _NEXT_REQUEST.get(service, now))
                spacing = interval * self.slowdown.get(service, 1)
                delay = slot - now
                # A long wait inherited from another run must fit this run's
                # budget. Later attempts have already waited on their own 429.
                cooling = service == 'airbnb' and attempt == 1 and delay > spacing + 1
                if not cooling or delay <= self.retry_budget_seconds - self.retry_waited_seconds:
                    _NEXT_REQUEST[service] = slot + spacing
            if cooling:
                if not self.wait_retry(delay, f'{service} provider cooldown'):
                    error = HTTPError('', 429, f'{service} cooldown exceeds the retry wait budget', {}, None)
                    error.collection_operation = operation
                    raise error
            else:
                self.wait(max(0., slot-time.monotonic()), f'Pacing {service} requests')
            try:
                return function()
            except Exception as exc:
                if operation:
                    exc.collection_operation = operation
                delay = retry_delay(exc, attempt)
                if retryable(exc) and http_status(exc) == 429:
                    self.slowdown[service] = min(8, self.slowdown.get(service, 1)*2)
                    with _LOCK:
                        _NEXT_REQUEST[service] = max(_NEXT_REQUEST.get(service, 0), time.monotonic()+delay)
                if not retryable(exc) or attempt == attempts:
                    raise
                if not self.wait_retry(delay, f'Temporary {service} error; retry {attempt+1}/{attempts}'):
                    raise
