from pathlib import Path

cli_path = Path("/home/nick/dev/verdict-core/verdict/cli.py")
text = cli_path.read_text(encoding="utf-8")

text = text.replace("def cmd_doctor() -> None:", "def cmd_doctor(fix: bool = False) -> None:")
text = text.replace("data = plane.export_manifest()", "data = plane.export_records()")
text = text.replace(
    'report = plane.import_manifest(data)\n        console.print(f"[bold green]✓ Imported {report.imported_records} record(s)[/bold green]")',
    'count = plane.import_records(data)\n        console.print(f"[bold green]✓ Imported {count} record(s)[/bold green]")',
)
text = text.replace(
    "adapter = CodeGraphAdapter()\n        rep = adapter.ingest_sqlite(db, plane)",
    "graph_adapter = CodeGraphAdapter()\n        rep = graph_adapter.ingest_sqlite(db, plane)",
)

cli_path.write_text(text, encoding="utf-8")
