import pytest
from pydantic import ValidationError

from sentinel_common.models import EventBatch, EventIn, Level


def test_level_is_case_insensitive():
    assert EventIn(service="api", level="error").level is Level.ERROR


def test_5xx_counts_as_error_even_with_info_level():
    assert EventIn(service="api", level="info", status_code=503).is_error
    assert not EventIn(service="api", level="info", status_code=404).is_error


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "semi;colon"])
def test_invalid_service_names_rejected(bad):
    with pytest.raises(ValidationError):
        EventIn(service=bad)


def test_negative_latency_rejected():
    with pytest.raises(ValidationError):
        EventIn(service="api", latency_ms=-1)


def test_empty_batch_rejected():
    with pytest.raises(ValidationError):
        EventBatch(events=[])
