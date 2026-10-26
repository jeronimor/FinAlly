"""Tests for the SSE router factory."""

from app.market import PriceCache, create_stream_router


def test_factory_creates_independent_routers():
    cache = PriceCache()
    r1 = create_stream_router(cache)
    r2 = create_stream_router(cache)
    assert r1 is not r2
    assert len([r for r in r1.routes if r.path == "/api/stream/prices"]) == 1
    assert len([r for r in r2.routes if r.path == "/api/stream/prices"]) == 1
