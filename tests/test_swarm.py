from verdict.swarm import initiate_swarm


def test_initiate_swarm():
    class DummyIntelligence:
        pass

    res = initiate_swarm("build UI", DummyIntelligence())  # type: ignore
    assert res["ready_for_architect"] is True
