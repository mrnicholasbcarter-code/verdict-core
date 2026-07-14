import time
import asyncio
from llm_gate.gate import Gate
from llm_gate.classifier import classify


async def run_benchmark():
    gate = Gate()

    # 1. Classifier test
    t0 = time.perf_counter()
    for _ in range(1000):
        classify("anthropic/claude-3-5-sonnet")
    t1 = time.perf_counter()
    classifier_lat = (t1 - t0) * 1000 / 1000

    # 2. Gate deterministic routing
    t2 = time.perf_counter()
    for i in range(1000):
        gate.route("test dummy prompt " + str(i), criticality="medium")
    t3 = time.perf_counter()
    router_lat = (t3 - t2) * 1000 / 1000

    print("## Benchmarks (v0.2.0)\n")
    print("| Component | Operations | Avg Latency |")
    print("| --- | --- | --- |")
    print(f"| Classifier | 1000 | {classifier_lat:.3f} ms/op |")
    print(f"| Gate Routing (Deterministic) | 1000 | {router_lat:.3f} ms/op |")


if __name__ == "__main__":
    asyncio.run(run_benchmark())
