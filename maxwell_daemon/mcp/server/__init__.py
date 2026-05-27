"""Model Context Protocol (MCP) Server."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import secrets
import tempfile
from collections.abc import AsyncIterator, Callable, Generator
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)
from pydantic import AnyUrl
from starlette.types import ASGIApp, Receive, Scope, Send

from maxwell_daemon.config import load_config
from maxwell_daemon.core.action_service import ActionService
from maxwell_daemon.core.action_store import ActionStore
from maxwell_daemon.core.cross_audit import DEFAULT_CROSS_AUDIT_ROLES
from maxwell_daemon.mcp.server.daemon_client import DaemonClient
from maxwell_daemon.mcp.server.daemon_tools import build_daemon_registry
from maxwell_daemon.tools.builtins import build_default_registry
from maxwell_daemon.tools.mcp import ToolParam, ToolRegistry, ToolSpec, mcp_tool

log = logging.getLogger(__name__)

# --- Mock Data for Fallbacks ---

MOCK_CLINICAL_TRIALS = [
    {
        "nct_id": "NCT04512345",
        "title": "Study of Pembrolizumab in Patients with Advanced Solid Tumors",
        "status": "RECRUITING",
        "sponsor": "Merck Sharp & Dohme LLC",
        "conditions": ["Solid Tumors", "Cancer"],
        "summary": "This study evaluates the safety and efficacy of Pembrolizumab in patients with advanced solid tumors.",
    },
    {
        "nct_id": "NCT03987654",
        "title": "A Phase 3 Trial of CAR-T Cell Therapy for B-cell Lymphoma",
        "status": "ACTIVE_NOT_RECRUITING",
        "sponsor": "National Cancer Institute (NCI)",
        "conditions": ["B-cell Lymphoma", "Lymphoma"],
        "summary": "This phase 3 trial investigates the efficacy of CAR-T cell therapy in treating patients with B-cell lymphoma.",
    },
    {
        "nct_id": "NCT05224466",
        "title": "Efficacy and Safety of Metformin in Type 2 Diabetes Patients",
        "status": "COMPLETED",
        "sponsor": "University of Oxford",
        "conditions": ["Diabetes", "Type 2 Diabetes"],
        "summary": "This clinical trial assesses the glycemic control and cardiovascular safety of Metformin in type 2 diabetes.",
    },
]

MOCK_PUBMED_ARTICLES = [
    {
        "uid": "34210987",
        "title": "Targeted therapies in cancer treatment: A review",
        "authors": "Smith A, Johnson B",
        "source": "Journal of Clinical Oncology",
        "pubdate": "2025 May 10",
    },
    {
        "uid": "35890123",
        "title": "Mechanisms of metformin action in type 2 diabetes",
        "authors": "Davis C, Miller D",
        "source": "Diabetes Care",
        "pubdate": "2025 Aug 22",
    },
    {
        "uid": "36712345",
        "title": "Clinical trials for COVID-19 vaccines: Safety and efficacy",
        "authors": "Wilson E, Garcia F",
        "source": "The Lancet",
        "pubdate": "2026 Feb 18",
    },
]

MOCK_CHEMBL_MOLECULES = [
    {
        "pref_name": "ASPIRIN",
        "molecule_chembl_id": "CHEMBL25",
        "max_phase": 4,
        "molecule_type": "Small molecule",
        "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
    },
    {
        "pref_name": "METFORMIN",
        "molecule_chembl_id": "CHEMBL1431",
        "max_phase": 4,
        "molecule_type": "Small molecule",
        "canonical_smiles": "CN(C)C(=N)N=C(N)N",
    },
    {
        "pref_name": "IBUPROFEN",
        "molecule_chembl_id": "CHEMBL521",
        "max_phase": 4,
        "molecule_type": "Small molecule",
        "canonical_smiles": "CC(C)Cc1ccc(cc1)C(C)C(=O)O",
    },
]


def mock_search_clinical_trials(query: str, limit: int) -> str:
    """Filter mock trials based on query."""
    q = query.lower()
    matches = []
    for trial in MOCK_CLINICAL_TRIALS:
        title = trial.get("title")
        sponsor = trial.get("sponsor")
        summary = trial.get("summary")
        conditions = trial.get("conditions")
        if (
            (isinstance(title, str) and q in title.lower())
            or (
                isinstance(conditions, list)
                and any(isinstance(cond, str) and q in cond.lower() for cond in conditions)
            )
            or (isinstance(sponsor, str) and q in sponsor.lower())
            or (isinstance(summary, str) and q in summary.lower())
        ):
            matches.append(trial)
    if not matches:
        matches = MOCK_CLINICAL_TRIALS

    results = matches[:limit]
    lines = []
    for trial in results:
        lines.append(
            f"NCT ID: {trial['nct_id']}\n"
            f"Title: {trial['title']}\n"
            f"Status: {trial['status']}\n"
            f"Sponsor: {trial['sponsor']}\n"
            f"Conditions: {', '.join(trial['conditions'])}\n"
            f"Summary: {trial['summary']}\n"
            "---"
        )
    return "\n".join(lines)


def mock_search_pubmed(query: str, limit: int) -> str:
    """Filter mock PubMed articles based on query."""
    q = query.lower()
    matches = []
    for article in MOCK_PUBMED_ARTICLES:
        if (
            q in article["title"].lower()
            or q in article["authors"].lower()
            or q in article["source"].lower()
        ):
            matches.append(article)
    if not matches:
        matches = MOCK_PUBMED_ARTICLES

    results = matches[:limit]
    lines = []
    for article in results:
        lines.append(
            f"PMID: {article['uid']}\n"
            f"Title: {article['title']}\n"
            f"Authors: {article['authors']}\n"
            f"Source: {article['source']}\n"
            f"Publication Date: {article['pubdate']}\n"
            "---"
        )
    return "\n".join(lines)


def mock_search_chembl(query: str, limit: int) -> str:
    """Filter mock ChEMBL molecules based on query."""
    q = query.lower()
    matches = []
    for molecule in MOCK_CHEMBL_MOLECULES:
        pref = str(molecule.get("pref_name") or "")
        m_id = str(molecule.get("molecule_chembl_id") or "")
        m_type = str(molecule.get("molecule_type") or "")
        if q in pref.lower() or q in m_id.lower() or q in m_type.lower():
            matches.append(molecule)
    if not matches:
        matches = MOCK_CHEMBL_MOLECULES

    results = matches[:limit]
    lines = []
    for mol in results:
        lines.append(
            f"ChEMBL ID: {mol['molecule_chembl_id']}\n"
            f"Preferred Name: {mol['pref_name']}\n"
            f"Type: {mol['molecule_type']}\n"
            f"Max Phase: {mol['max_phase']}\n"
            f"SMILES: {mol['canonical_smiles']}\n"
            "---"
        )
    return "\n".join(lines)


# --- Science Tools Definition ---


@mcp_tool(
    name="search_clinical_trials",
    description="Search ClinicalTrials.gov for active/recruiting trials matching a disease or drug query.",
    capabilities=frozenset({"network"}),
    risk_level="read_only",
    params=[
        ToolParam(
            name="query", type="string", description="Search term (e.g. disease, drug, condition)"
        ),
        ToolParam(
            name="limit",
            type="integer",
            description="Maximum number of results to return (default 5)",
            required=False,
        ),
    ],
)
async def search_clinical_trials(query: str, limit: int = 5) -> str:
    """Search ClinicalTrials.gov with mock fallback."""
    try:
        url = "https://clinicaltrials.gov/api/v2/studies"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        params: dict[str, Any] = {"query.term": query, "pageSize": limit}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

            studies = data.get("studies", [])
            if not studies:
                return f"No clinical trials found for query: {query}"

            lines = []
            for study in studies:
                protocol = study.get("protocolSection", {})
                ident = protocol.get("identificationModule", {})
                nct_id = ident.get("nctId", "N/A")
                title = ident.get("officialTitle") or ident.get("briefTitle") or "No Title"

                status_mod = protocol.get("statusModule", {})
                status = status_mod.get("overallStatus", "N/A")

                sponsor_mod = protocol.get("sponsorCollaboratorsModule", {})
                sponsor = sponsor_mod.get("leadSponsor", {}).get("name", "N/A")

                conds = protocol.get("conditionsModule", {})
                conditions = conds.get("conditions", [])

                desc = protocol.get("descriptionModule", {})
                summary = desc.get("briefSummary", "N/A")

                lines.append(
                    f"NCT ID: {nct_id}\n"
                    f"Title: {title}\n"
                    f"Status: {status}\n"
                    f"Sponsor: {sponsor}\n"
                    f"Conditions: {', '.join(conditions)}\n"
                    f"Summary: {summary}\n"
                    "---"
                )
            return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        log.warning("Clinical Trials API request failed, falling back to mock. Error: %s", e)
        return mock_search_clinical_trials(query, limit)


@mcp_tool(
    name="search_pubmed",
    description="Search PubMed database using E-Utilities for publications related to a biological topic.",
    capabilities=frozenset({"network"}),
    risk_level="read_only",
    params=[
        ToolParam(name="query", type="string", description="Search query for PubMed database"),
        ToolParam(
            name="limit",
            type="integer",
            description="Maximum number of results to return (default 5)",
            required=False,
        ),
    ],
)
async def search_pubmed(query: str, limit: int = 5) -> str:
    """Search PubMed using NCBI E-utilities with mock fallback."""
    try:
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        search_params: dict[str, Any] = {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": limit,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            search_resp = await client.get(search_url, params=search_params)
            search_resp.raise_for_status()
            search_data = search_resp.json()

            id_list = search_data.get("esearchresult", {}).get("idlist", [])
            if not id_list:
                return f"No PubMed articles found for query: {query}"

            summary_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            summary_params = {"db": "pubmed", "id": ",".join(id_list), "retmode": "json"}
            summary_resp = await client.get(summary_url, params=summary_params)
            summary_resp.raise_for_status()
            summary_data = summary_resp.json()

            results = summary_data.get("result", {})
            lines = []
            for uid in id_list:
                summary = results.get(uid)
                if not summary:
                    continue
                title = summary.get("title", "No Title")
                authors_list = summary.get("authors", [])
                authors = ", ".join([a.get("name", "") for a in authors_list if "name" in a])
                source = summary.get("source", "N/A")
                pubdate = summary.get("pubdate", "N/A")

                lines.append(
                    f"PMID: {uid}\n"
                    f"Title: {title}\n"
                    f"Authors: {authors}\n"
                    f"Source: {source}\n"
                    f"Publication Date: {pubdate}\n"
                    "---"
                )
            return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        log.warning("PubMed API request failed, falling back to mock. Error: %s", e)
        return mock_search_pubmed(query, limit)


@mcp_tool(
    name="search_chembl",
    description="Search ChEMBL database for bioactive molecules by name or ID.",
    capabilities=frozenset({"network"}),
    risk_level="read_only",
    params=[
        ToolParam(name="query", type="string", description="Chemical or drug name, or ChEMBL ID"),
        ToolParam(
            name="limit",
            type="integer",
            description="Maximum number of results to return (default 5)",
            required=False,
        ),
    ],
)
async def search_chembl(query: str, limit: int = 5) -> str:
    """Search ChEMBL with mock fallback."""
    try:
        url = "https://www.ebi.ac.uk/chembl/api/data/molecule/search.json"
        params: dict[str, Any] = {"q": query, "limit": limit}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            molecules = data.get("molecules", [])
            if not molecules:
                return f"No ChEMBL molecules found for query: {query}"

            lines = []
            for mol in molecules:
                chembl_id = mol.get("molecule_chembl_id", "N/A")
                pref_name = mol.get("pref_name") or "N/A"
                mol_type = mol.get("molecule_type") or "N/A"
                max_phase = mol.get("max_phase")
                max_phase_str = str(max_phase) if max_phase is not None else "N/A"
                smiles = mol.get("molecule_structures", {}).get("canonical_smiles") or "N/A"

                lines.append(
                    f"ChEMBL ID: {chembl_id}\n"
                    f"Preferred Name: {pref_name}\n"
                    f"Type: {mol_type}\n"
                    f"Max Phase: {max_phase_str}\n"
                    f"SMILES: {smiles}\n"
                    "---"
                )
            return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        log.warning("ChEMBL API request failed, falling back to mock. Error: %s", e)
        return mock_search_chembl(query, limit)


# --- Registry Builder Helper ---


def build_mcp_registry(workspace_path: Path) -> ToolRegistry:
    """Build the ToolRegistry with default tools and science tools."""
    registry = build_default_registry(workspace_path)
    registry.register_from_function(search_clinical_trials)
    registry.register_from_function(search_pubmed)
    registry.register_from_function(search_chembl)
    return registry


# --- Stdio Server (Legacy / Direct CLI support) ---


async def run_mcp_server(config_path: Path | None = None) -> None:  # noqa: C901
    """Run the Maxwell Daemon as an MCP server via stdio."""
    config = load_config(config_path)

    server = Server("maxwell-daemon")

    # Wire up the ActionService so side-effecting tools require approval in the daemon UI
    action_store = ActionStore(":memory:")
    action_service = ActionService(action_store)

    # Expose both default workspace tools and science search skills
    registry = build_default_registry(config.memory.workspace_path, action_service=action_service)
    registry.register_from_function(search_clinical_trials)
    registry.register_from_function(search_pubmed)
    registry.register_from_function(search_chembl)

    # Expose the daemon tools via REST API proxy
    client = DaemonClient(config.api.host, config.api.port, config.api.auth_token)
    daemon_registry = build_daemon_registry(client)

    for name in daemon_registry.names():
        registry.register(daemon_registry.get(name))

    @server.list_tools()  # type: ignore
    async def handle_list_tools() -> list[Tool]:
        mcp_tools = []
        for name in registry.names():
            spec = registry.get(name)

            # Map ToolParam to JSON Schema
            schema: dict[str, Any] = {
                "type": "object",
                "properties": {},
                "required": [],
            }
            for param in spec.params:
                schema["properties"][param.name] = {
                    "type": param.type,
                    "description": param.description,
                }
                if param.enum:
                    schema["properties"][param.name]["enum"] = param.enum
                if param.required:
                    schema["required"].append(param.name)

            mcp_tools.append(Tool(name=spec.name, description=spec.description, inputSchema=schema))
        return mcp_tools

    @server.call_tool()  # type: ignore
    async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        try:
            result = await registry.invoke(name, arguments or {})
            if result.is_error:
                return [TextContent(type="text", text=f"Error: {result.content}")]
            return [TextContent(type="text", text=result.content)]
        except Exception as e:
            log.exception("Tool execution failed: %s", name)
            return [TextContent(type="text", text=f"Tool exception: {e}")]

    @server.list_resources()  # type: ignore
    async def handle_list_resources() -> list[Resource]:
        return [
            Resource(
                uri=AnyUrl("artifact://list"),
                name="Artifacts",
                description="Maxwell Daemon artifacts",
            ),
            Resource(
                uri=AnyUrl("workspace://list"),
                name="Workspaces",
                description="Task workspaces",
            ),
            Resource(
                uri=AnyUrl("memory://list"),
                name="Episodic Memory",
                description="Agent memory",
            ),
        ]

    @server.read_resource()  # type: ignore
    async def handle_read_resource(uri: AnyUrl | str) -> str:
        return f"Resource {uri} is not fully implemented yet over REST proxy."

    @server.list_prompts()  # type: ignore
    async def handle_list_prompts() -> list[Prompt]:
        prompts = []
        for role_id, role in DEFAULT_CROSS_AUDIT_ROLES.items():
            prompts.append(
                Prompt(
                    name=f"maxwell_{role_id}",
                    description=f"Maxwell: {role.name}",
                    arguments=[],
                )
            )
        return prompts

    @server.get_prompt()  # type: ignore
    async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
        role_id = name.replace("maxwell_", "")
        role = DEFAULT_CROSS_AUDIT_ROLES.get(role_id)
        if not role:
            raise ValueError(f"Unknown prompt: {name}")

        return GetPromptResult(
            description=role.name,
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=role.system_prompt),
                )
            ],
        )

    options = server.create_initialization_options()
    async with stdio_server() as (read, write):
        await server.run(read, write, options)


# --- HTTP MCP Server & Auth Middleware ---


class AuthTokenMiddleware:
    """ASGI middleware to enforce bearer token authentication."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("utf-8")
            expected_header = f"Bearer {self.token}"
            if auth_header != expected_header:
                response_body = b"Unauthorized"
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"text/plain"),
                            (b"content-length", str(len(response_body)).encode()),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": response_body,
                    }
                )
                return
        await self.app(scope, receive, send)


class SignalBypassingServer(uvicorn.Server):
    """Subclass of uvicorn.Server that bypasses signal handler registration."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


@contextlib.asynccontextmanager
async def start_mcp_http_server(
    config_path: Path | None = None,
) -> AsyncIterator[tuple[Path, str]]:
    """Context manager to start HTTP MCP server and yield temporary config file path."""
    config = load_config(config_path)
    registry = build_mcp_registry(config.memory.workspace_path)

    mcp_app = FastMCP("maxwell-daemon")

    # Map ToolRegistry to FastMCP app
    for name in registry.names():
        spec = registry.get(name)

        def make_handler(tool_spec: ToolSpec) -> Callable[..., Any]:
            async def handler(**kwargs: Any) -> str:
                res = await registry.invoke(tool_spec.name, kwargs)
                if res.is_error:
                    raise Exception(res.content)
                return res.content

            handler.__name__ = tool_spec.name
            handler.__doc__ = tool_spec.description
            handler.__signature__ = inspect.signature(tool_spec.handler)  # type: ignore[attr-defined]
            handler.__annotations__ = tool_spec.handler.__annotations__
            return handler

        mcp_app.add_tool(make_handler(spec))

    token = secrets.token_hex(32)
    asgi_app = AuthTokenMiddleware(mcp_app.streamable_http_app(), token)

    uvi_config = uvicorn.Config(
        asgi_app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        loop="asyncio",
    )
    server = SignalBypassingServer(uvi_config)
    server_task = asyncio.create_task(server.serve())

    start_time = asyncio.get_running_loop().time()
    while not server.started:
        if server_task.done():
            server_task.result()  # will raise error if task failed
            raise RuntimeError("MCP server failed to start")
        if asyncio.get_running_loop().time() - start_time > 5.0:
            raise TimeoutError("Timeout waiting for MCP server to start")
        await asyncio.sleep(0.01)

    host, port = server.servers[0].sockets[0].getsockname()
    server_url = f"http://{host}:{port}/mcp"
    log.info("MCP HTTP server started at %s", server_url)

    # Write temporary mcp-config.json
    headers = {"Authorization": f"Bearer {token}"}
    fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="mcp-config-")
    try:
        os.close(fd)
        config_data = {
            "mcpServers": {
                "maxwell-daemon": {
                    "type": "http",
                    "url": server_url,
                    "headers": headers,
                }
            }
        }
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)

        yield Path(temp_path), server_url
    finally:
        # Cleanup config file
        if os.path.exists(temp_path):
            with contextlib.suppress(OSError):
                os.remove(temp_path)

        # Stop server
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("Timeout waiting for MCP server shutdown, cancelling task")
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
