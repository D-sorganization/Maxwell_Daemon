# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- Audit and fix noqa suppressions for security-relevant codes (#712)

## [0.1.0] - 2026-04-26

### Added
- Initial release of Maxwell-Daemon
- FastAPI-based HTTP API for daemon control plane
- SQLite-backed action and task storage
- JWT and static token authentication
- GitHub webhook integration
- Fleet management capabilities
- SSH session management (optional asyncssh)
- Audit logging with SHA-256 chain verification
- Prometheus metrics and Grafana dashboards
- Multiple LLM backend adapters (Anthropic, OpenAI, Azure, DeepSeek, Gemini, Groq, HuggingFace, Mistral, Together, OpenRouter, Ollama)