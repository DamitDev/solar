"""Model tests for the partial status message update.

``body``, ``type`` and ``start_time`` are non-nullable columns, and the database
update treats a null in those fields as "unchanged". An explicit null must
therefore fail validation instead of silently doing nothing, while an explicit
``end_time: null`` stays meaningful: it makes the window open-ended.
"""

import pytest
from pydantic import ValidationError

from app.models.status_message import StatusMessageUpdate


class TestStatusMessageUpdateNulls:
    @pytest.mark.parametrize("field", ["body", "type", "start_time"])
    def test_explicit_null_is_rejected(self, field):
        with pytest.raises(ValidationError) as excinfo:
            StatusMessageUpdate.model_validate({field: None})

        errors = excinfo.value.errors()
        assert [error["loc"] for error in errors] == [(field,)]
        assert "must not be null" in errors[0]["msg"]

    def test_end_time_null_is_accepted_and_marks_the_field_set(self):
        update = StatusMessageUpdate.model_validate({"end_time": None})

        assert update.end_time is None
        assert update.model_fields_set == {"end_time"}

    def test_omitted_fields_stay_unchanged(self):
        update = StatusMessageUpdate.model_validate({"body": "Maintenance extended"})

        assert update.body == "Maintenance extended"
        assert update.model_fields_set == {"body"}
        assert update.type is None
        assert update.start_time is None
        assert update.end_time is None
