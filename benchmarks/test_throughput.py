"""
Throughput and latency benchmark for llm-gate.
Ensures proxying via the gateway overhead is minimal and scales.
"""

import asyncio
import time

from llm_gate.gate import Gate


async def benchmark():
    gate = Gate()
    time.perf_context()  # dummy
    # We would run ~1000 simulated prompt classifications here
    t0 = time.time()
    for i in range(1000):
        gate.route("test dummy prompt " + str(i), 0)
    t1 = time.time()
    print(f"1000 routes took {t1 - t0:.4f}s. Avg: {(t1 - t0) / 1000 * 1000:.2f}ms/op")


if __name__ == "__main__":
    asyncio.run(benchmark())
