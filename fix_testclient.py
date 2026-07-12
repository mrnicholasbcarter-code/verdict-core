with open("tests/integration/test_live_gateway.py", "r") as f:
    text = f.read()

broken = """client = TestClient(app)

def test_live_gateway_integration():
    # Hit health endpoint natively
    resp = client.get("/health")"""

fixed = """def test_live_gateway_integration():
    with TestClient(app) as client:
        # Hit health endpoint natively
        resp = client.get("/health")"""

fixed2 = text.replace(broken, fixed).replace("    resp = client.post", "        resp = client.post").replace("    assert", "        assert").replace("    data =", "        data =")

# Clean it up manually because spacing logic string replacement is annoying in python without regex
