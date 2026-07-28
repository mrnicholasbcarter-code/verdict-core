"""Unit tests for verdict.code_graph."""

from pathlib import Path

from verdict.code_graph import CodeGraphEngine
from verdict.memory_plane import MemoryPlane


def test_code_graph_engine_comprehensive(tmp_path: Path) -> None:
    # 1. Create a mini Python codebase
    main_py = tmp_path / "main.py"
    main_py.write_text(
        """
import math
from utils import helper

class BaseService:
    pass

class DataProcessor(BaseService):
    def process(self):
        helper()
        return math.sqrt(16)
""",
        encoding="utf-8",
    )

    test_py = tmp_path / "tests" / "test_main.py"
    test_py.parent.mkdir()
    test_py.write_text(
        """
from main import DataProcessor

def test_data_processor():
    proc = DataProcessor()
    assert proc.process() == 4.0
""",
        encoding="utf-8",
    )

    # 2. Parse directory
    engine = CodeGraphEngine()
    count = engine.parse_directory(tmp_path)
    assert count >= 2

    # 3. Test Query Patterns
    summary = engine.file_summary("main.py")
    assert len(summary) >= 2  # BaseService, DataProcessor

    inheritors = engine.inheritors_of("BaseService")
    assert len(inheritors) >= 1
    assert inheritors[0].name == "DataProcessor"

    callers = engine.callers_of("helper")
    assert len(callers) >= 1

    imports = engine.imports_of("main.py")
    assert "math" in imports or "utils" in imports

    importers = engine.importers_of("main")
    assert "tests/test_main.py" in importers

    tests = engine.tests_for("DataProcessor")
    assert len(tests) >= 1

    # 4. Test Analytics (hubs, bridges, impact radius)
    hubs = engine.hub_nodes(top_n=5)
    assert len(hubs) >= 1

    bridges = engine.bridge_nodes(top_n=5)
    assert len(bridges) >= 1

    impact = engine.get_impact_radius(["main.py"], max_depth=2)
    assert len(impact) >= 1

    # 5. Sync to MemoryPlane
    db_path = tmp_path / "memory.db"
    plane = MemoryPlane(path=db_path)
    synced = engine.sync_to_memory_plane(plane)
    assert synced >= 3

    records = plane.search("DataProcessor")
    assert len(records) >= 1
    plane.close()
