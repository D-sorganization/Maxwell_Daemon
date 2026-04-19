# Backend interface

Every LLM adapter implements `conductor.backends.ILLMBackend`:

```python
class ILLMBackend(ABC):
    name: str

    @abstractmethod
    async def complete(self, messages, *, model, ...) -> BackendResponse: ...

    @abstractmethod
    def stream(self, messages, *, model, ...) -> AsyncIterator[str]: ...

    @abstractmethod
    async def health_check(self) -> bool: ...

    @abstractmethod
    def capabilities(self, model: str) -> BackendCapabilities: ...

    def estimate_cost(self, usage: TokenUsage, model: str) -> float: ...
```

## Adding a new backend

1. Create `conductor/backends/<name>.py`:

    ```python
    from conductor.backends.base import ILLMBackend, ...
    from conductor.backends.registry import registry

    class MyBackend(ILLMBackend):
        name = "mybackend"
        ...

    registry.register("mybackend", MyBackend)
    ```

2. Add `"<name>"` to `_BUILTIN_BACKENDS` in `conductor/backends/registry.py` so autoload picks it up.

3. Declare capabilities honestly — `BackendCapabilities` drives routing decisions and cost estimation, so a wrong number here breaks budget alerts silently.

4. Add a test file. Mock the HTTP transport (e.g. with `respx` for `httpx`-based backends). Don't hit real APIs in unit tests.

## Reference implementations

- `conductor/backends/claude.py` — uses the Anthropic SDK, splits system messages out of the thread, records cache-read tokens when the provider returns them.
- `conductor/backends/openai.py` — also covers any OpenAI-compatible server (point `base_url` at a local vLLM/LM Studio/LocalAI endpoint and drop the API key).
- `conductor/backends/azure.py` — 52-line subclass of `OpenAIBackend` that swaps in `AsyncAzureOpenAI`. Demonstrates the payoff of interface-driven design.
- `conductor/backends/ollama.py` — raw `httpx` against the Ollama HTTP API. Zero-cost local inference, streaming support.
