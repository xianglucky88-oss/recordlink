import pytest

def test_blocking_is_open_gap():
    from src.recordlink import blocking
    with pytest.raises(NotImplementedError):
        blocking.process({"id": "x"})

def test_arbitration_is_open_gap():
    from src.recordlink import arbitration
    with pytest.raises(NotImplementedError):
        arbitration.process({"id": "x"})
