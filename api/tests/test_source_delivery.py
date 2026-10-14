"""Delivery identity validation must precede any database connection."""
import pytest
from screener.source_observations import SourceObservationRepository
from screener.source_validity import load_delivery

@pytest.mark.parametrize("field",["occurrence_id","source_item_id","source_observation_id"])
@pytest.mark.parametrize("value",[True,0,-1,"1",1.5])
def test_delivery_ids_reject_invalid_before_database(field,value):
    values=dict(occurrence_id=1,source_item_id=1,source_observation_id=1)
    values[field]=value
    with pytest.raises(ValueError,match="positive integer"):
        SourceObservationRepository(None).delivery(**values)
    with pytest.raises(ValueError,match="positive integer"):
        load_delivery(None,**values)
