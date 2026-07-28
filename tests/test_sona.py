"""Unit tests for verdict.sona."""

from pathlib import Path

from verdict.memory_plane import MemoryPlane
from verdict.sona import SonaLearningEngine


def test_sona_learning_engine_adaptation_and_sync(tmp_path: Path) -> None:
    engine = SonaLearningEngine(ewc_lambda=0.5, lora_alpha=0.1)

    # 1. Register patterns
    p1 = engine.register_pattern("pat_auth_jwt", "auth", "JWT Bearer Token pattern")
    p2 = engine.register_pattern("pat_auth_oauth", "auth", "OAuth2 Flow pattern")

    assert p1.weight == 1.0
    assert p2.weight == 1.0

    # 2. Adapt patterns with feedback
    # Successful execution boosts pat_auth_jwt
    new_w1 = engine.adapt_pattern("pat_auth_jwt", feedback_score=1.0, execution_latency_ms=12.0)
    assert new_w1 > 1.0

    # Failed execution penalizes pat_auth_oauth
    new_w2 = engine.adapt_pattern("pat_auth_oauth", feedback_score=-1.0, execution_latency_ms=50.0)
    assert new_w2 < 1.0

    # 3. Predict best pattern
    best = engine.predict_best_pattern("auth")
    assert best is not None
    assert best.pattern_id == "pat_auth_jwt"

    # 4. Sync to MemoryPlane
    db_path = tmp_path / "memory.db"
    plane = MemoryPlane(path=db_path)
    synced = engine.sync_to_memory_plane(plane)
    assert synced == 2

    # 5. Load into fresh engine
    engine2 = SonaLearningEngine()
    loaded = engine2.load_from_memory_plane(plane)
    assert loaded == 2

    best2 = engine2.predict_best_pattern("auth")
    assert best2 is not None
    assert best2.pattern_id == "pat_auth_jwt"
    plane.close()
