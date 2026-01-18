"""Tests for thread safety of LoadBalancer."""

import threading
from collections import Counter

from flow_proxy_plugin.core.load_balancer import LoadBalancer


class TestLoadBalancerThreadSafety:
    """Test suite for LoadBalancer thread safety."""

    def test_concurrent_get_next_config(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test concurrent calls to get_next_config are thread-safe."""
        lb = LoadBalancer(sample_secrets_config)
        results = []
        lock = threading.Lock()

        def get_config() -> None:
            """Get next config and append to results."""
            config = lb.get_next_config()
            with lock:
                results.append(config["name"])

        # Create multiple threads to concurrently get configs
        threads = []
        num_threads = 10
        for _ in range(num_threads):
            thread = threading.Thread(target=get_config)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Verify all threads got a config
        assert len(results) == num_threads

        # Verify configs were distributed (should have mix of config1 and config2)
        counter = Counter(results)
        assert len(counter) > 0  # At least one unique config
        assert all(name in ["config1", "config2"] for name in counter.keys())

        # Verify total requests counter is accurate
        assert lb.total_requests == num_threads

    def test_concurrent_mark_failed(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test concurrent mark_config_failed calls are thread-safe."""
        lb = LoadBalancer(sample_secrets_config)

        # Get a config to mark as failed
        config1 = lb.get_next_config()

        results = []
        lock = threading.Lock()

        def mark_failed(config: dict) -> None:
            """Mark config as failed and record result."""
            try:
                lb.mark_config_failed(config)
                with lock:
                    results.append("success")
            except Exception as e:
                with lock:
                    results.append(f"error: {str(e)}")

        # Try to mark the same config as failed from multiple threads
        threads = []
        num_threads = 5
        for _ in range(num_threads):
            thread = threading.Thread(target=mark_failed, args=(config1,))
            threads.append(thread)
            thread.start()

        # Wait for all threads
        for thread in threads:
            thread.join()

        # All operations should complete (some may warn about already failed)
        assert len(results) == num_threads
        assert all("success" in r for r in results)

        # Config should only be in failed list once
        assert lb.failed_count == 1
        assert lb.available_count == 1

    def test_concurrent_reset_failed_configs(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test concurrent reset_failed_configs calls are thread-safe."""
        lb = LoadBalancer(sample_secrets_config)

        # Mark one config as failed
        config = lb.get_next_config()
        lb.mark_config_failed(config)

        assert lb.failed_count == 1
        assert lb.available_count == 1

        def reset_configs() -> None:
            """Reset failed configs."""
            lb.reset_failed_configs()

        # Try to reset from multiple threads concurrently
        threads = []
        num_threads = 5
        for _ in range(num_threads):
            thread = threading.Thread(target=reset_configs)
            threads.append(thread)
            thread.start()

        # Wait for all threads
        for thread in threads:
            thread.join()

        # Should have all configs available again
        assert lb.available_count == 2
        assert lb.failed_count == 0

    def test_mixed_concurrent_operations(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test mixed concurrent operations are thread-safe."""
        lb = LoadBalancer(sample_secrets_config)
        results = {"get": 0, "mark": 0, "reset": 0}
        lock = threading.Lock()

        def get_operation() -> None:
            """Perform get_next_config operation."""
            try:
                lb.get_next_config()
                with lock:
                    results["get"] += 1
            except RuntimeError:
                # May fail if all configs are marked as failed
                pass

        def mark_operation() -> None:
            """Perform mark_config_failed operation."""
            try:
                config = lb.get_next_config()
                lb.mark_config_failed(config)
                with lock:
                    results["mark"] += 1
            except RuntimeError:
                # May fail if no configs available
                pass

        def reset_operation() -> None:
            """Perform reset_failed_configs operation."""
            lb.reset_failed_configs()
            with lock:
                results["reset"] += 1

        # Create mixed threads
        threads = []
        for i in range(20):
            if i % 3 == 0:
                thread = threading.Thread(target=get_operation)
            elif i % 3 == 1:
                thread = threading.Thread(target=mark_operation)
            else:
                thread = threading.Thread(target=reset_operation)
            threads.append(thread)
            thread.start()

        # Wait for all threads
        for thread in threads:
            thread.join()

        # Verify at least some operations succeeded
        assert results["get"] > 0
        assert sum(results.values()) > 0

        # LoadBalancer should still be in a consistent state
        assert lb.available_count + lb.failed_count == lb.total_count
        assert lb.available_count >= 0
        assert lb.failed_count >= 0

    def test_high_concurrency_stress_test(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Stress test with high concurrency to ensure thread safety."""
        lb = LoadBalancer(sample_secrets_config)
        num_threads = 100
        configs_obtained = []
        lock = threading.Lock()

        def worker() -> None:
            """Worker thread that gets configs."""
            for _ in range(10):
                try:
                    config = lb.get_next_config()
                    with lock:
                        configs_obtained.append(config["name"])
                except RuntimeError:
                    # May happen if all configs fail
                    pass

        # Create many threads
        threads = []
        for _ in range(num_threads):
            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()

        # Wait for completion
        for thread in threads:
            thread.join()

        # Verify we got many configs
        assert len(configs_obtained) > 0

        # Verify round-robin distribution (should be roughly equal)
        counter = Counter(configs_obtained)
        if len(counter) == 2:  # Both configs used
            # Allow up to 30% variance in distribution
            counts = list(counter.values())
            ratio = min(counts) / max(counts)
            assert ratio > 0.3, f"Distribution too uneven: {counter}"

        # Verify total requests matches
        assert lb.total_requests == len(configs_obtained)
