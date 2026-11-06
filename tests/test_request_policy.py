import requests
import pytest
from src import request_policy as rp


def error(code, **headers):
    response = requests.Response()
    response.status_code = code
    response.headers.update(headers)
    response._content = b'{}'
    return requests.HTTPError(response=response)


def test_retry_after_is_honored_and_transient_call_retried(monkeypatch):
    monkeypatch.setattr(rp, '_NEXT_REQUEST', {})
    policy = rp.RequestPolicy(rp.automatic_options())
    waits = []
    monkeypatch.setattr(policy, 'wait', lambda delay, message: waits.append((delay, message)))
    attempts = []
    def call():
        attempts.append(1)
        if len(attempts) == 1:
            raise error(429, **{'Retry-After': '17'})
        return 'ok'
    assert policy.call(call) == 'ok'
    assert len(attempts) == 2
    assert any(delay == 17 for delay, _ in waits)


def test_retries_are_bounded_and_permanent_errors_not_retried(monkeypatch):
    monkeypatch.setattr(rp, '_NEXT_REQUEST', {})
    policy = rp.RequestPolicy(rp.automatic_options())
    monkeypatch.setattr(policy, 'wait', lambda *args: None)
    for code, expected in [(503, 3), (401, 1), (403, 1)]:
        calls = []
        def fail():
            calls.append(1)
            raise error(code)
        with pytest.raises(requests.HTTPError):
            policy.call(fail)
        assert len(calls) == expected


def test_billing_quota_is_not_retryable():
    exc = error(429)
    exc.response._content = b'{"error":{"code":"insufficient_quota"}}'
    assert not rp.retryable(exc)


def test_cancel_during_cooldown_is_immediate():
    policy = rp.RequestPolicy(rp.automatic_options(), should_cancel=lambda: True)
    with pytest.raises(rp.RunCancelled):
        policy.wait(60, 'cooldown')


@pytest.mark.parametrize('count,budget', [(1,1200), (23,1380), (100,3600)])
def test_retry_wait_budget_scales_and_stops(monkeypatch, count, budget):
    policy = rp.RequestPolicy(rp.automatic_options(), property_count=count)
    monkeypatch.setattr(policy, 'wait', lambda *args: None)
    assert policy.retry_budget_seconds == budget
    assert policy.wait_retry(budget, 'wait')
    assert not policy.wait_retry(1, 'stop')
    assert policy.retry_stop_reason


def test_urllib_rate_limit_is_retried_and_slows_pacing(monkeypatch):
    from urllib.error import HTTPError
    monkeypatch.setattr(rp, '_NEXT_REQUEST', {})
    policy = rp.RequestPolicy(rp.automatic_options())
    monkeypatch.setattr(policy, 'wait', lambda *args: None)
    calls = []
    def invoke():
        calls.append(1)
        if len(calls) < 3:
            raise HTTPError('https://example.invalid', 429, 'Too Many Requests', {'Retry-After': '60'}, None)
        return 'ok'
    assert policy.call(invoke) == 'ok'
    assert policy.retry_waited_seconds == 120
    assert policy.slowdown['airbnb'] == 4


def test_retry_wait_publishes_deadline_and_clears_it_on_cancel(monkeypatch):
    observed = []
    cancelled = [False]
    policy = rp.RequestPolicy(rp.automatic_options(), should_cancel=lambda: cancelled[0])
    def progress(message):
        observed.append((message, policy.wait_state.copy() if policy.wait_state else None))
        cancelled[0] = True
    policy.progress = progress
    monkeypatch.setattr(rp.threading.Event, 'wait', lambda *args: None)
    before = rp.time.time()
    with pytest.raises(rp.RunCancelled):
        policy.wait_retry(60, 'Temporary error; retry 2/3')
    assert observed[0][1]['kind'] == 'retry'
    assert before+59 <= observed[0][1]['until'] <= before+61
    assert policy.wait_state is None
    assert observed[-1][1] is None


def test_default_airbnb_pacing_covers_individual_requests(monkeypatch):
    monkeypatch.setattr(rp,'_NEXT_REQUEST',{})
    policy = rp.RequestPolicy(rp.automatic_options())
    waits = []
    monkeypatch.setattr(policy,'wait',lambda seconds,message: waits.append(seconds))
    policy.call(lambda:'page')
    policy.call(lambda:'price')
    assert waits[-1] >= 2.9
