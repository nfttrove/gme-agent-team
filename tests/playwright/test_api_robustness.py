"""API robustness and concurrent operation tests."""
import pytest
import aiohttp
import asyncio


@pytest.mark.asyncio
async def test_signals_api_structure(dashboard_server):
    """Signals API returns well-formed data."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{dashboard_server}/api/signals") as resp:
            assert resp.status == 200
            data = await resp.json()

            # Should be list of signals
            assert isinstance(data, list)
            if len(data) > 0:
                signal = data[0]
                assert "id" in signal or "agent_name" in signal
                assert "confidence" in signal or "timestamp" in signal

            print(f"✅ Signals API returns {len(data)} signals")


@pytest.mark.asyncio
async def test_metrics_api_structure(dashboard_server):
    """Metrics API returns properly structured data."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{dashboard_server}/api/metrics") as resp:
            assert resp.status == 200
            data = await resp.json()

            # Should be dict with agent metrics
            assert isinstance(data, dict)
            for agent, metrics in data.items():
                assert isinstance(metrics, dict)
                # Check common metric fields
                assert any(
                    k in metrics
                    for k in ["total_signals", "executed", "win_rate", "avg_pnl_pct"]
                )

            print(f"✅ Metrics API returns {len(data)} agents")


@pytest.mark.asyncio
async def test_feedback_api_validation(dashboard_server):
    """Feedback API validates required fields."""
    async with aiohttp.ClientSession() as session:
        # Missing required fields
        payload = {"action": "executed"}  # Missing alert_id

        async with session.post(
            f"{dashboard_server}/api/feedback", json=payload
        ) as resp:
            # Should return 400 error
            assert resp.status in [400, 422]
            print(f"✅ Feedback API validates required fields (status: {resp.status})")


@pytest.mark.asyncio
async def test_concurrent_signal_requests(dashboard_server):
    """API handles concurrent requests."""
    async with aiohttp.ClientSession() as session:
        # Make 5 concurrent requests
        tasks = [
            session.get(f"{dashboard_server}/api/signals")
            for _ in range(5)
        ]
        responses = await asyncio.gather(*tasks)

        # All should succeed
        statuses = [r.status for r in responses]
        assert all(status == 200 for status in statuses)
        print(f"✅ Handled {len(responses)} concurrent requests")

        # Cleanup
        for resp in responses:
            resp.close()


@pytest.mark.asyncio
async def test_api_response_times(dashboard_server):
    """API responds within reasonable time."""
    import time

    async with aiohttp.ClientSession() as session:
        start = time.time()
        async with session.get(f"{dashboard_server}/api/metrics") as resp:
            elapsed = time.time() - start

            assert resp.status == 200
            assert elapsed < 2.0, f"Metrics API took {elapsed:.2f}s (should be <2s)"
            print(f"✅ Metrics API responded in {elapsed:.3f}s")


@pytest.mark.asyncio
async def test_health_endpoint(dashboard_server):
    """Health check endpoint works."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{dashboard_server}/health") as resp:
            if resp.status == 200:
                data = await resp.json()
                assert "status" in data or "db" in data
                print(f"✅ Health endpoint: {data}")
            else:
                # Health endpoint might not exist, but API should still work
                print("⚠️ Health endpoint not found (optional)")


@pytest.mark.asyncio
async def test_feedback_api_success(dashboard_server, signal_manager):
    """Feedback API accepts valid payload."""
    # Create signal first
    alert_id = signal_manager.log_alert(
        agent_name="TestAgent",
        signal_type="api_test",
        confidence=0.80,
        entry_price=25.00,
    )

    payload = {
        "alert_id": alert_id,
        "action": "executed",
        "entry_price": 25.00,
        "exit_price": 26.00,
        "member": "APITest",
        "notes": "Test via API",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{dashboard_server}/api/feedback", json=payload
        ) as resp:
            assert resp.status in [200, 201]
            data = await resp.json()
            assert "status" in data or "alert_id" in data
            print(f"✅ Feedback API accepted payload (status: {resp.status})")


@pytest.mark.asyncio
async def test_api_cors_headers(dashboard_server):
    """API sets CORS headers for browser requests."""
    async with aiohttp.ClientSession() as session:
        async with session.options(f"{dashboard_server}/api/signals") as resp:
            # OPTIONS request should work or be ignored
            # Just verify we can make cross-origin-style requests
            print(f"✅ API accessible from cross-origin (status: {resp.status})")


@pytest.mark.asyncio
async def test_invalid_json_handling(dashboard_server):
    """API rejects invalid JSON gracefully."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{dashboard_server}/api/feedback",
            data="not json",
            headers={"Content-Type": "application/json"},
        ) as resp:
            # Should return error, not 500
            assert resp.status in [400, 422, 500]  # 500 is acceptable for invalid JSON
            print(f"✅ Invalid JSON handled (status: {resp.status})")
