from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    from mcp.server import MCPServer
except ImportError:  # pragma: no cover - alternate public import in early 2.x builds
    from mcp.server.mcpserver import MCPServer

from knowledge_service import KnowledgeService, SpaceRegistry


def _registry_path() -> Path | None:
    value = str(os.environ.get("LOCALT_REGISTRY_PATH") or "").strip()
    return Path(value).expanduser().resolve() if value else None


service = KnowledgeService(SpaceRegistry(_registry_path()))
mcp = MCPServer(
    "LocalTranscriber Knowledge",
    version="2.0.0",
    instructions=(
        "This server exposes user-approved local video knowledge spaces. "
        "Always pass space_id explicitly. Use search results only as candidates, "
        "then cite returned evidence_ids. Do not invent facts when evidence is absent."
    ),
    log_level="WARNING",
)


@mcp.tool()
def list_knowledge_spaces() -> dict[str, Any]:
    """List knowledge spaces authorized in the LocalTranscriber GUI without loading their indexes."""
    values = service.list_spaces()
    return {"count": len(values), "spaces": values}


@mcp.tool()
def get_space_catalog(space_id: str, domain: str = "", limit: int = 100) -> dict[str, Any]:
    """List domains, video sources and concepts in one authorized space. This loads only that space."""
    return service.catalog(space_id, domain=domain, limit=limit)


@mcp.tool()
def search_knowledge(
    space_id: str,
    query: str,
    limit: int = 8,
    domain: str = "",
    source_id: str = "",
    include_related: bool = True,
) -> dict[str, Any]:
    """Search trusted local knowledge. Call repeatedly with rewritten queries when evidence is insufficient."""
    return service.search(
        space_id,
        query,
        limit=limit,
        domain=domain,
        source_id=source_id,
        include_related=include_related,
    )


@mcp.tool()
def get_evidence(space_id: str, evidence_id: str) -> dict[str, Any]:
    """Read one permanent trusted transcript evidence unit and its video timestamp."""
    return service.get_evidence(space_id, evidence_id)


@mcp.tool()
def expand_evidence_context(
    space_id: str,
    evidence_id: str,
    before: int = 2,
    after: int = 2,
) -> dict[str, Any]:
    """Read adjacent trusted transcript units from the same video around one evidence_id."""
    return service.expand_evidence_context(space_id, evidence_id, before=before, after=after)


@mcp.tool()
def get_related_concepts(space_id: str, concept_id: str, limit: int = 20) -> dict[str, Any]:
    """Return direct, evidence-backed concept relations from one knowledge space."""
    return service.get_related_concepts(space_id, concept_id, limit=limit)


@mcp.tool()
def open_video_evidence(space_id: str, evidence_id: str) -> dict[str, Any]:
    """Open a visible local video player at the evidence timestamp. This performs a local UI action."""
    return service.open_video_evidence(space_id, evidence_id)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
