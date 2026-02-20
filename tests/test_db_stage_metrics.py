from app.db import _filter_stage_metrics
from app.models.data_model import IngestionStage


def test_filter_stage_metrics_keeps_skipped_precheck_flag():
    job = {
        "stages": {
            IngestionStage.PRECHECK.value: {
                "status": "completed",
                "metrics": {"skipped": True, "unsupported_key": 123},
            }
        }
    }

    filtered = _filter_stage_metrics(job)
    assert filtered["stages"][IngestionStage.PRECHECK.value]["metrics"] == {"skipped": True}
