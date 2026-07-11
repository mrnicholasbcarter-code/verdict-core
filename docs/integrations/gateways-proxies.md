# Enterprise Gateways & Proxies (Portkey, OmniRoute, Peezy)

For enterprise deployments or centralized team proxies, `llm-gate` acts as the security/criticality "brain." Because we don't proxy the byte-stream natively, you pair `llm-gate` with your proxy of choice.

### Portkey AI Integration
[Portkey](https://portkey.ai) handles unified routing and observability. You can use Portkey's Custom Webhook Routing or Virtual Keys to dynamically fetch the target model from a local `llm-gate` microservice before executing the proxy request.

### OmniRoute & 9router
When running [OmniRoute](https://github.com/diegosouzapw/OmniRoute), map `llm-gate` alongside it:
```yaml
services:
  omniroute:
    image: omniroute:latest
    ports:
      - "20129:20128" # Avoid defaults
    environment:
      - OMNIROUTE_MAX_PENDING_MIGRATIONS=0
      - GATE = http://llm-gate:8000/v1/route
```

### Peezy Gateway (p0.systems)
Similar to OmniRoute, configure Peezy's upstream evaluation hook to query `http://localhost:8000/v1/route` (from `llm-gate serve`) to dictate model fallback thresholds.
