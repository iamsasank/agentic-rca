# API Gateway High Error Rate Runbook

When API gateway error rate increases with JVM heap pressure, inspect host memory, GC pause time, recent cache growth, and deployment changes.

If `OutOfMemoryError` references `RequestCacheManager`, check whether request cache eviction is enabled and whether sustained traffic can grow the cache without bounds.

# Payment Processor Network Degradation Runbook

When payment calls are slow but west coast probes remain healthy, compare ThousandEyes paths by region. East coast packet loss isolated to an ISP peering point should be mitigated by shifting traffic to a healthy payment route.

# Correlation Guidance

Host memory alerts and synthetic network packet loss can be independent overlapping failures. Treat them as separate root causes when each has direct evidence and a separate mitigation.
