"""Did the bank accept the call? engine.succeeded reads the success flag the spec takes from the tool API file;
without one, a 2xx answer without an error field is a success. Written after mutation testing (mutmut) showed the
other tests pass even when the spec's flag is ignored or the error check is inverted."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from builder.runtime import engine  # noqa: E402


def ok(responses, data, node="api_promise", status=200):
    engine.S = {"responses": responses}
    V = {f"{node}_response.data.{k}": v for k, v in data.items()}
    if status is not None:
        V[f"{node}_response.status_code"] = status
    return engine.succeeded(V, node)


def test_the_spec_flag_and_its_values_decide():
    r = {"create_promise": {"field": "status", "values": ["created"]}}
    assert ok(r, {"status": "created"}) is True
    assert ok(r, {"status": "Created "}) is True
    assert ok(r, {"status": "rejected"}) is False
    assert ok(r, {"status": "rejected", "ok": True}) is False          # the default 'ok' field is not the flag here
    assert ok(r, {"status": "created"}, node="api_promise_2") is True   # chained nodes use their tool's flag
    assert ok({"add_note": {"field": "status", "values": ["created"]}}, {"ok": False}) is False   # other tool's flag


def test_a_flag_without_values_is_read_as_a_boolean():
    r = {"create_promise": {"field": "accepted"}}
    assert ok(r, {"accepted": True}) is True
    assert ok(r, {"accepted": "false"}) is False
    assert ok(r, {"accepted": False, "ok": True}) is False


def test_without_a_spec_flag_ok_then_the_error_field_decide():
    assert ok({}, {"ok": True}) is True
    assert ok({}, {"ok": False}) is False
    assert ok({}, {"done": True}) is True                              # no flag, no error: accepted
    assert ok({}, {"error": "identity not verified"}) is False
    assert ok({"create_promise": {"field": "", "values": []}}, {"error": "x"}) is False
    assert ok({"create_promise": {"field": "", "values": []}}, {"done": True}) is True


def test_an_http_failure_or_an_empty_answer_is_never_a_success():
    assert ok({}, {"ok": True}, status=500) is False
    assert ok({}, {}) is False
    assert ok({"create_promise": {"field": "status", "values": ["created"]}}, {"status": "created"}, status=503) is False
