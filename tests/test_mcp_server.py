from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from knowledge_service import KnowledgeService, SpaceRegistry
from tests.test_knowledge_service import build_space


MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(MCP_AVAILABLE, "optional MCP dependency is not installed")
class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_lists_and_calls_the_read_only_tools(self):
        from mcp import Client
        import localtranscriber_mcp as server_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space, _copied, entries = build_space(root)
            registry = SpaceRegistry(root / "state" / "spaces.json")
            registered = registry.register(space, entries=entries)
            original_service = server_module.service
            server_module.service = KnowledgeService(registry)
            try:
                async with Client(server_module.mcp) as client:
                    listed = await client.list_tools()
                    names = {item.name for item in listed.tools}
                    spaces = await client.call_tool("list_knowledge_spaces", {})
                    search = await client.call_tool(
                        "search_knowledge",
                        {"space_id": registered["space_id"], "query": "膝盖方向"},
                    )
            finally:
                server_module.service = original_service

            self.assertEqual(
                names,
                {
                    "list_knowledge_spaces",
                    "get_space_catalog",
                    "search_knowledge",
                    "get_evidence",
                    "expand_evidence_context",
                    "get_related_concepts",
                    "open_video_evidence",
                },
            )
            self.assertEqual(spaces.structured_content["count"], 1)
            self.assertGreaterEqual(search.structured_content["count"], 1)
            self.assertTrue(search.structured_content["results"][0]["evidence_ids"])


if __name__ == "__main__":
    unittest.main()
