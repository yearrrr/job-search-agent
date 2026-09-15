import json

import httpx
import pytest
from pydantic import SecretStr

from job_search_agent.agent.model import DeepSeekModel, ModelError
from job_search_agent.db import Database
from job_search_agent.probe import PROBE_MESSAGES, check_api

FAKE_KEY = "fictional-test-key-DO-NOT-USE"


def live_settings(settings):
    return settings.model_copy(update={"mode": "deepseek", "api_key": SecretStr(FAKE_KEY)})


def success_payload(content="OK", usage=None):
    result = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    if usage is not None:
        result["usage"] = usage
    return result


@pytest.mark.parametrize(
    "mode,key,expected",
    [("mock", FAKE_KEY, "network_disabled"), ("deepseek", "", "missing_key")],
)
def test_credentials_and_mode_block_network(settings, mode, key, expected):
    configured = settings.model_copy(update={"mode": mode, "api_key": SecretStr(key)})
    with pytest.raises(ModelError) as error:
        DeepSeekModel(configured).complete(PROBE_MESSAGES)
    assert error.value.code == expected
    assert not settings.data_dir.exists()


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_request"),
        (401, "authentication"),
        (402, "balance"),
        (422, "invalid_request"),
        (429, "rate_limit"),
        (500, "unavailable"),
        (503, "unavailable"),
        (302, "http_error"),
    ],
)
def test_http_errors_are_bounded_and_safe(settings, caplog, status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            text=f"provider echo: {FAKE_KEY}",
            headers={"Location": "https://invalid.example/"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = DeepSeekModel(live_settings(settings), client)
        with pytest.raises(ModelError) as error:
            model.complete(PROBE_MESSAGES)
    assert len(requests) == 1
    assert error.value.code == code
    assert FAKE_KEY not in str(error.value) + caplog.text


@pytest.mark.parametrize(
    "exception,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")]
)
def test_transport_errors_do_not_echo_exception(settings, exception, code):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise exception(FAKE_KEY, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as error:
            DeepSeekModel(live_settings(settings), client).complete(PROBE_MESSAGES)
    assert count == 1 and error.value.code == code
    assert FAKE_KEY not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"choices": []},
        {"choices": [{"finish_reason": "stop", "message": 42}]},
        {"choices": [{"finish_reason": "tool_calls", "message": {"content": "missing tools"}}]},
        {"choices": [{"finish_reason": "length", "message": {"content": "cut"}}]},
        success_payload(""),
        success_payload("OK", {"total_tokens": -1}),
    ],
)
def test_invalid_outputs(settings, payload):
    def handler(request):
        return httpx.Response(
            200,
            content=b"not-json" if payload is None else json.dumps(payload).encode(),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as error:
            DeepSeekModel(live_settings(settings), client).complete(PROBE_MESSAGES)
    assert error.value.code == "invalid_output"


def test_tool_call_parsing(settings):
    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "id": "t1",
                            "function": {
                                "name": "search_demo_evidence",
                                "arguments": '{"query":"Python"}',
                            },
                        }
                    ],
                },
            }
        ]
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        reply = DeepSeekModel(live_settings(settings), client).complete(PROBE_MESSAGES)
    assert reply.tool_calls[0].arguments == {"query": "Python"}
    assert reply.usage.total_tokens is None


@pytest.mark.parametrize(
    "usage", [None, {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}]
)
def test_probe_only_sends_fixed_text_and_deduplicates(settings, usage, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=success_payload("OK", usage))

    configured = live_settings(settings)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = DeepSeekModel(configured, client)
        result = check_api(configured, model, probe_id="same-probe")
        repeated = check_api(configured, model, probe_id="same-probe")
    body = json.loads(requests[0].content)
    assert body["messages"] == PROBE_MESSAGES
    assert body["max_tokens"] == 32 and body["thinking"] == {"type": "disabled"}
    assert str(requests[0].url) == DeepSeekModel.ENDPOINT
    assert requests[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert len(requests) == 1 and result == repeated
    assert result["status"] == "ok"
    assert result["total_tokens"] == (12 if usage else None)
    assert FAKE_KEY not in json.dumps(result) + caplog.text
    assert FAKE_KEY.encode() not in Database(settings.data_dir).path.read_bytes()


def test_probe_failure_is_recorded_without_response_body(settings):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text=FAKE_KEY))
    ) as client:
        result = check_api(live_settings(settings), DeepSeekModel(live_settings(settings), client))
    assert result["status"] == "failed" and result["error_code"] == "authentication"
    assert result["total_tokens"] is None


def test_input_limit_is_checked_before_network(settings):
    with pytest.raises(ModelError) as error:
        DeepSeekModel(live_settings(settings)).complete([{"role": "user", "content": "x" * 21000}])
    assert error.value.code == "invalid_request"
