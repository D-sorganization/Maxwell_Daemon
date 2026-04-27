def test_smoke(benchmark):
    benchmark(lambda: sum(range(100)))

