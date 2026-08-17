from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import knowledge_service as service_module
from knowledge_service import KnowledgeService, SpaceRegistry
from knowledge_space import copy_video, load_index, publish_verified, write_index


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def complete_json(self, _system, _payload, max_tokens=0):
        return self.responses.pop(0), {}


def build_space(root: Path) -> tuple[Path, dict, list[dict]]:
    space = root / "space"
    source = root / "lesson.mp4"
    source.write_bytes(b"service-video")
    copied = copy_video(space, source)
    verified = root / "lesson.verified.json"
    verified.write_text(
        json.dumps(
            {
                "segments": [
                    {"id": "0", "start": 1, "end": 4, "final_text": "深蹲时膝盖与脚尖方向一致。", "knowledge_ready": True},
                    {"id": "1", "start": 4, "end": 8, "final_text": "动作过程中保持足底稳定。", "knowledge_ready": True},
                    {"id": "2", "start": 8, "end": 12, "final_text": "如果疼痛应停止训练并检查动作。", "knowledge_ready": True},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    publish_verified(
        space,
        Path(copied["path"]),
        verified,
        FakeClient(
            [
                {
                    "domain": "健身",
                    "wiki_title": "深蹲课程",
                    "summary": "",
                    "knowledge_points": [
                        {
                            "title": "深蹲膝盖方向",
                            "aliases": ["膝盖轨迹"],
                            "content": "深蹲时膝盖应与脚尖方向一致。",
                            "segment_ids": ["0"],
                            "relations": [{"type": "related_to", "target": "足底稳定"}],
                        },
                        {
                            "title": "足底稳定",
                            "aliases": [],
                            "content": "深蹲过程中需要保持足底稳定。",
                            "segment_ids": ["1"],
                        },
                    ],
                }
            ]
        ),
    )
    return space, copied, load_index(space)


class KnowledgeServiceTests(unittest.TestCase):
    def test_registry_listing_is_lightweight_and_hides_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space, _copied, entries = build_space(root)
            registry = SpaceRegistry(root / "state" / "spaces.json")
            registered = registry.register(space, entries=entries)

            with patch.object(service_module, "load_index", side_effect=AssertionError("list must not load index")):
                values = registry.list_spaces()

            self.assertEqual(values[0]["space_id"], registered["space_id"])
            self.assertEqual(values[0]["knowledge_count"], 2)
            self.assertNotIn("root", values[0])
            self.assertNotIn(str(space.resolve()), json.dumps(values, ensure_ascii=False))

    def test_space_id_survives_moving_the_whole_space(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space, _copied, entries = build_space(root)
            registry = SpaceRegistry(root / "state" / "spaces.json")
            first = registry.register(space, entries=entries)
            moved = root / "moved-space"
            shutil.move(str(space), str(moved))

            second = registry.register(moved, entries=entries)
            resolved, _record = registry.resolve(first["space_id"])

            self.assertEqual(second["space_id"], first["space_id"])
            self.assertEqual(resolved, moved.resolve())
            self.assertEqual(len(registry.list_spaces()), 1)

    def test_search_cache_loads_once_and_returns_structured_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space, copied, entries = build_space(root)
            registry = SpaceRegistry(root / "state" / "spaces.json")
            registered = registry.register(space, entries=entries)
            service = KnowledgeService(registry)

            first = service.search(registered["space_id"], "膝盖轨迹")
            second = service.search(registered["space_id"], "足底稳定")
            diagnostics = service.cache_diagnostics(registered["space_id"])

            self.assertGreaterEqual(first["count"], 1)
            self.assertEqual(first["results"][0]["source_id"], copied["video_id"])
            self.assertTrue(first["results"][0]["evidence_ids"])
            self.assertNotIn("video_path", first["results"][0])
            self.assertGreaterEqual(second["count"], 1)
            self.assertEqual(diagnostics["load_counts"]["index"], 1)
            self.assertEqual(diagnostics["load_counts"]["evidence"], 1)

    def test_only_changed_index_part_is_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space, _copied, entries = build_space(root)
            registry = SpaceRegistry(root / "state" / "spaces.json")
            registered = registry.register(space, entries=entries)
            service = KnowledgeService(registry)
            service.search(registered["space_id"], "深蹲")
            updated = [dict(item) for item in entries]
            updated[0]["content"] = "深蹲时膝盖轨迹应保持稳定。"
            write_index(space, updated)

            service.search(registered["space_id"], "轨迹稳定")
            diagnostics = service.cache_diagnostics(registered["space_id"])

            self.assertEqual(diagnostics["load_counts"]["index"], 2)
            self.assertEqual(diagnostics["load_counts"]["evidence"], 1)
            self.assertEqual(diagnostics["load_counts"]["concepts"], 1)
            self.assertEqual(diagnostics["load_counts"]["relations"], 1)

    def test_evidence_context_and_related_concepts_are_composable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space, _copied, entries = build_space(root)
            registry = SpaceRegistry(root / "state" / "spaces.json")
            registered = registry.register(space, entries=entries)
            service = KnowledgeService(registry)
            search = service.search(registered["space_id"], "膝盖方向")
            first = search["results"][0]
            context = service.expand_evidence_context(registered["space_id"], first["evidence_ids"][0], before=1, after=1)
            relations = service.get_related_concepts(registered["space_id"], first["concept_id"])

            self.assertTrue(any(item["is_target"] for item in context["evidence"]))
            self.assertGreaterEqual(len(context["evidence"]), 2)
            self.assertTrue(any(item["name"] == "足底稳定" for item in relations["related"]))

    def test_unregistered_space_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = KnowledgeService(SpaceRegistry(Path(directory) / "spaces.json"))
            with self.assertRaises(KeyError):
                service.search("space-" + "0" * 32, "深蹲")

    def test_parallel_readers_share_one_immutable_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space, _copied, entries = build_space(root)
            registry = SpaceRegistry(root / "state" / "spaces.json")
            registered = registry.register(space, entries=entries)
            service = KnowledgeService(registry)

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda query: service.search(registered["space_id"], query), ["深蹲", "膝盖", "足底", "疼痛"]))

            self.assertTrue(all(item["count"] >= 1 for item in results))
            self.assertEqual(service.cache_diagnostics(registered["space_id"])["load_counts"]["index"], 1)

    def test_multiple_agent_service_instances_can_read_the_same_space(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space, _copied, entries = build_space(root)
            registry = SpaceRegistry(root / "state" / "spaces.json")
            registered = registry.register(space, entries=entries)
            first = KnowledgeService(registry)
            second = KnowledgeService(registry)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda item: item[0].search(registered["space_id"], item[1]),
                        [(first, "膝盖"), (second, "足底")],
                    )
                )

            self.assertTrue(all(item["count"] >= 1 for item in results))
            self.assertEqual(first.cache_diagnostics(registered["space_id"])["load_counts"]["index"], 1)
            self.assertEqual(second.cache_diagnostics(registered["space_id"])["load_counts"]["index"], 1)


if __name__ == "__main__":
    unittest.main()
