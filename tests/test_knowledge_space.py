from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import knowledge_space as knowledge_module

from knowledge_space import (
    INDEX_NAME,
    OBSIDIAN_FOLDER,
    VIDEO_FOLDER,
    answer_question,
    clean_task_work,
    copy_video,
    create_task,
    discover_videos,
    initialize_space,
    load_claims,
    load_concepts,
    load_evidence_units,
    load_index,
    publish_verified,
    rebuild_obsidian_wiki,
    relink_video,
    search_index,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, system, payload, max_tokens=0):
        self.calls.append({"system": system, "payload": payload, "max_tokens": max_tokens})
        return self.responses.pop(0), {}


class KnowledgeSpaceTests(unittest.TestCase):
    def test_concept_lookup_is_built_once_for_a_multi_concept_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"
            source = root / "lesson.mp4"
            source.write_bytes(b"lookup-once")
            copied = copy_video(space, source)
            verified = root / "lesson.verified.json"
            verified.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"id": "0", "start": 1, "end": 2, "final_text": "概念甲关联概念乙。", "knowledge_ready": True},
                            {"id": "1", "start": 3, "end": 4, "final_text": "概念丙独立存在。", "knowledge_ready": True},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            client = FakeClient(
                [
                    {
                        "domain": "测试",
                        "wiki_title": "查找表测试",
                        "summary": "",
                        "knowledge_points": [
                            {"title": "概念甲", "content": "概念甲关联概念乙。", "segment_ids": ["0"], "relations": [{"type": "related_to", "target": "概念乙"}]},
                            {"title": "概念丙", "content": "概念丙独立存在。", "segment_ids": ["1"]},
                        ],
                    }
                ]
            )

            with patch.object(knowledge_module, "_concept_lookup", wraps=knowledge_module._concept_lookup) as lookup:
                publish_verified(space, Path(copied["path"]), verified, client)

            self.assertEqual(lookup.call_count, 1)

    def test_completed_legacy_migration_and_projection_are_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"
            initialize_space(space)
            (space / INDEX_NAME).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "knowledge_id": "knowledge-legacy-once",
                        "domain": "AI",
                        "title": "一次迁移",
                        "content": "旧数据只迁移一次。",
                        "video_id": "video-legacy-once",
                        "video_fingerprint": "legacy-once",
                        "video_relative_path": "视频/旧数据.mp4",
                        "evidence_start": 1,
                        "evidence_end": 2,
                        "evidence_text": "旧数据只迁移一次。",
                        "obsidian_relative_path": "Obsidian知识库/AI/旧数据.md",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            (space / ".knowledge" / "metadata.json").unlink()

            with (
                patch.object(knowledge_module, "_upgrade_legacy_knowledge", wraps=knowledge_module._upgrade_legacy_knowledge) as migrate,
                patch.object(knowledge_module, "_render_obsidian_wiki", wraps=knowledge_module._render_obsidian_wiki) as render,
            ):
                first = rebuild_obsidian_wiki(space)
                index_mtime = (space / INDEX_NAME).stat().st_mtime_ns
                map_mtime = (space / OBSIDIAN_FOLDER / "知识地图.md").stat().st_mtime_ns
                second = rebuild_obsidian_wiki(space)

            metadata = json.loads((space / ".knowledge" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(first, second)
            self.assertEqual(migrate.call_count, 1)
            self.assertEqual(render.call_count, 1)
            self.assertEqual((space / INDEX_NAME).stat().st_mtime_ns, index_mtime)
            self.assertEqual((space / OBSIDIAN_FOLDER / "知识地图.md").stat().st_mtime_ns, map_mtime)
            self.assertEqual(metadata["legacy_migration_version"], knowledge_module.LEGACY_MIGRATION_VERSION)
            self.assertEqual(metadata["obsidian_projection_version"], knowledge_module.OBSIDIAN_PROJECTION_VERSION)

    def test_second_video_updates_only_its_affected_obsidian_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"

            def publish(name: str, domain: str, concept: str, content: str):
                source = root / f"{name}.mp4"
                source.write_bytes(name.encode("utf-8"))
                copied = copy_video(space, source)
                verified = root / f"{name}.verified.json"
                verified.write_text(
                    json.dumps({"segments": [{"id": "0", "start": 1, "end": 2, "final_text": content, "knowledge_ready": True}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                result = publish_verified(
                    space,
                    Path(copied["path"]),
                    verified,
                    FakeClient([{"domain": domain, "wiki_title": name, "summary": "", "knowledge_points": [{"title": concept, "content": content, "segment_ids": ["0"]}]}]),
                    domain_hint=domain,
                )
                return result

            first = publish("第一课", "领域甲", "概念甲", "第一课知识。")
            with patch.object(knowledge_module, "_atomic_write_text", wraps=knowledge_module._atomic_write_text) as atomic_write:
                second = publish("第二课", "领域乙", "概念乙", "第二课知识。")

            obsidian_targets = {
                Path(call.args[0]).resolve()
                for call in atomic_write.call_args_list
                if OBSIDIAN_FOLDER in Path(call.args[0]).parts
            }
            first_source = Path(first["wiki"]).resolve()
            first_concept = (space / OBSIDIAN_FOLDER / "概念" / "概念甲.md").resolve()
            second_source = Path(second["wiki"]).resolve()
            second_concept = (space / OBSIDIAN_FOLDER / "概念" / "概念乙.md").resolve()
            self.assertNotIn(first_source, obsidian_targets)
            self.assertNotIn(first_concept, obsidian_targets)
            self.assertIn(second_source, obsidian_targets)
            self.assertIn(second_concept, obsidian_targets)

    def test_folder_discovery_is_recursive_and_deduplicates_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "AI" / "课程.mp4"
            second = root / "健身" / "动作.mov"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first-video")
            second.write_bytes(b"second-video")
            (root / "说明.txt").write_text("ignore", encoding="utf-8")

            values = discover_videos([root, first])

            self.assertEqual([item["name"] for item in values], ["课程.mp4", "动作.mov"])

    def test_copy_uses_fingerprint_and_does_not_duplicate_the_same_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "课程.mp4"
            space = root / "space"
            source.parent.mkdir()
            source.write_bytes(b"video-content")

            first = copy_video(space, source)
            second = copy_video(space, source)

            self.assertTrue(first["copied"])
            self.assertFalse(second["copied"])
            self.assertEqual(first["video_id"], second["video_id"])
            self.assertEqual(len(list((space / VIDEO_FOLDER).glob("*.mp4"))), 1)

    def test_verified_video_publishes_obsidian_and_searchable_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "AI知识空间"
            source = root / "Agent课程.mp4"
            source.write_bytes(b"agent-video")
            copied = copy_video(space, source)
            verified = root / "lesson.verified.json"
            verified.write_text(
                json.dumps(
                    {
                        "source_video": copied["path"],
                        "segments": [
                            {
                                "id": "0",
                                "start": 12.5,
                                "end": 18.2,
                                "final_text": "Agent 可以调用经过授权的工具。",
                                "knowledge_ready": True,
                            },
                            {
                                "id": "1",
                                "start": 20,
                                "end": 25,
                                "final_text": "可能存在错误的内容。",
                                "knowledge_ready": False,
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            client = FakeClient(
                [
                    {
                        "domain": "AI",
                        "wiki_title": "Agent 工具调用",
                        "summary": "介绍 Agent 的工具能力。",
                        "knowledge_points": [
                            {
                                "title": "Agent 如何调用工具",
                                "content": "Agent 只能调用经过授权的工具。",
                                "segment_ids": ["0"],
                            }
                        ],
                    }
                ]
            )

            published = publish_verified(space, Path(copied["path"]), verified, client, domain_hint="人工智能")
            hits = search_index(space, "Agent 调用工具")

            self.assertEqual(published["knowledge_count"], 1)
            self.assertTrue((space / INDEX_NAME).is_file())
            self.assertEqual(published["domain"], "人工智能")
            source_note = space / OBSIDIAN_FOLDER / "人工智能" / "Agent 工具调用.md"
            concept_note = space / OBSIDIAN_FOLDER / "概念" / "Agent 如何调用工具.md"
            self.assertTrue(source_note.is_file())
            self.assertTrue(concept_note.is_file())
            self.assertTrue((space / OBSIDIAN_FOLDER / "领域" / "人工智能.md").is_file())
            self.assertTrue((space / OBSIDIAN_FOLDER / "知识地图.md").is_file())
            self.assertTrue((space / OBSIDIAN_FOLDER / ".obsidian" / "graph.json").is_file())
            self.assertIn("[[概念/Agent 如何调用工具|Agent 如何调用工具]]", source_note.read_text(encoding="utf-8"))
            self.assertIn("[[人工智能/Agent 工具调用|Agent 工具调用]]", concept_note.read_text(encoding="utf-8"))
            indexed = load_index(space)[0]
            self.assertEqual(indexed["domain"], "人工智能")
            self.assertEqual(indexed["obsidian_relative_path"], "Obsidian知识库/概念/Agent 如何调用工具.md")
            self.assertEqual(indexed["source_obsidian_relative_path"], "Obsidian知识库/人工智能/Agent 工具调用.md")
            self.assertEqual(hits[0]["evidence_start"], 12.5)
            self.assertTrue(hits[0]["video_available"])
            self.assertNotIn("可能存在错误的内容", json.dumps(load_index(space), ensure_ascii=False))

    def test_same_concept_from_two_videos_converges_into_one_obsidian_node(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"
            for index, (name, statement) in enumerate(
                [
                    ("第一课.mp4", "训练需要逐步增加负荷。"),
                    ("第二课.mp4", "渐进负荷也需要安排恢复。"),
                ],
                start=1,
            ):
                source = root / name
                source.write_bytes(f"video-{index}".encode("ascii"))
                copied = copy_video(space, source)
                verified = root / f"lesson-{index}.verified.json"
                verified.write_text(
                    json.dumps(
                        {
                            "segments": [
                                {
                                    "id": "0",
                                    "start": index * 10,
                                    "end": index * 10 + 5,
                                    "final_text": statement,
                                    "knowledge_ready": True,
                                }
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
                                "domain": "训练",
                                "wiki_title": f"训练第{index}课",
                                "summary": statement,
                                "knowledge_points": [
                                    {
                                        "title": "渐进负荷",
                                        "content": statement,
                                        "segment_ids": ["0"],
                                    }
                                ],
                            }
                        ]
                    ),
                )

            entries = load_index(space)
            concept_paths = {item["obsidian_relative_path"] for item in entries}
            concept_note = space / OBSIDIAN_FOLDER / "概念" / "渐进负荷.md"
            content = concept_note.read_text(encoding="utf-8")

            self.assertEqual(len(entries), 2)
            self.assertEqual(concept_paths, {"Obsidian知识库/概念/渐进负荷.md"})
            self.assertEqual(len(list((space / OBSIDIAN_FOLDER / "概念").glob("渐进负荷*.md"))), 1)
            self.assertIn("[[训练/训练第1课|训练第1课]]", content)
            self.assertIn("[[训练/训练第2课|训练第2课]]", content)
            self.assertIn("00:00:10–00:00:15", content)
            self.assertIn("00:00:20–00:00:25", content)

    def test_aliases_merge_cross_video_concepts_and_claim_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"
            for index, (name, title, aliases) in enumerate(
                [
                    ("第一课.mp4", "肌细胞", ["肌肉细胞"]),
                    ("第二课.mp4", "肌肉细胞", []),
                ],
                start=1,
            ):
                source = root / name
                source.write_bytes(f"video-{index}".encode("ascii"))
                copied = copy_video(space, source)
                verified = root / f"alias-{index}.verified.json"
                verified.write_text(
                    json.dumps(
                        {
                            "segments": [
                                {
                                    "id": "0",
                                    "start": index,
                                    "end": index + 1,
                                    "final_text": "肌细胞能够储存肌糖原。",
                                    "knowledge_ready": True,
                                }
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
                                "domain": "运动科学",
                                "wiki_title": name,
                                "summary": "",
                                "knowledge_points": [
                                    {
                                        "title": title,
                                        "aliases": aliases,
                                        "content": "肌细胞能够储存肌糖原。",
                                        "segment_ids": ["0"],
                                    }
                                ],
                            }
                        ]
                    ),
                )

            concepts = load_concepts(space)
            claims = load_claims(space)
            entries = load_index(space)

            self.assertEqual(len(concepts), 1)
            self.assertEqual(concepts[0]["canonical_name"], "肌细胞")
            self.assertIn("肌肉细胞", concepts[0]["aliases"])
            self.assertEqual({item["concept_id"] for item in entries}, {concepts[0]["concept_id"]})
            self.assertEqual(len(claims), 1)
            self.assertEqual(len(claims[0]["source_ids"]), 2)
            self.assertEqual(len(claims[0]["evidence_ids"]), 2)

    def test_trusted_transcript_is_permanent_and_unextracted_text_remains_searchable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"
            source = root / "课程.mp4"
            source.write_bytes(b"permanent-evidence-video")
            copied = copy_video(space, source)
            verified = root / "permanent.verified.json"
            verified.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"id": "0", "start": 1, "end": 3, "final_text": "课程介绍肌肉结构。", "knowledge_ready": True},
                            {"id": "1", "start": 12, "end": 18, "final_text": "卫星细胞参与骨骼肌修复。", "knowledge_ready": True},
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
                            "domain": "运动科学",
                            "wiki_title": "肌肉结构",
                            "summary": "",
                            "knowledge_points": [
                                {"title": "肌肉结构", "content": "课程介绍肌肉结构。", "segment_ids": ["0"]}
                            ],
                        }
                    ]
                ),
            )

            hits = search_index(space, "卫星细胞修复")
            evidence = load_evidence_units(space)

            self.assertEqual(len(evidence), 2)
            self.assertEqual(hits[0]["record_type"], "trusted_evidence")
            self.assertEqual(hits[0]["evidence_start"], 12)
            self.assertEqual(hits[0]["video_id"], copied["video_id"])
            durable = space / ".knowledge" / "sources" / copied["video_id"] / "transcript.verified.json"
            self.assertTrue(durable.is_file())
            self.assertTrue((space / OBSIDIAN_FOLDER / "index.md").is_file())
            self.assertTrue((space / OBSIDIAN_FOLDER / "log.md").is_file())

    def test_existing_flat_wiki_can_be_upgraded_from_index_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"
            initialize_space(space)
            legacy_source = space / OBSIDIAN_FOLDER / "AI" / "旧课程.md"
            legacy_source.parent.mkdir(parents=True)
            legacy_source.write_text(
                "---\ngenerated_by: LocalTranscriber\n---\n\n# 旧课程\n\n这段旧摘要需要保留。\n\n## Agent 工具\n\n旧内容。\n",
                encoding="utf-8",
            )
            (space / INDEX_NAME).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "knowledge_id": "knowledge-legacy",
                        "domain": "AI",
                        "title": "Agent 工具",
                        "content": "Agent 可以调用工具。",
                        "video_id": "video-legacy",
                        "video_fingerprint": "legacy",
                        "video_relative_path": "视频/旧课程.mp4",
                        "evidence_start": 3,
                        "evidence_end": 8,
                        "evidence_text": "Agent 可以调用经过授权的工具。",
                        "obsidian_relative_path": "Obsidian知识库/AI/旧课程.md",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = rebuild_obsidian_wiki(space)
            upgraded_source = legacy_source.read_text(encoding="utf-8")
            concept = (space / OBSIDIAN_FOLDER / "概念" / "Agent 工具.md").read_text(encoding="utf-8")

            self.assertEqual(result, {"knowledge_count": 1, "source_count": 1, "concept_count": 1})
            self.assertIn("这段旧摘要需要保留。", upgraded_source)
            self.assertIn("[[概念/Agent 工具|Agent 工具]]", upgraded_source)
            self.assertIn("[[AI/旧课程|旧课程]]", concept)

    def test_answer_only_returns_model_selected_local_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"
            source = root / "课程.mp4"
            source.write_bytes(b"video")
            copied = copy_video(space, source)
            verified = root / "lesson.verified.json"
            verified.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"id": "0", "start": 1, "end": 4, "final_text": "深蹲时膝盖与脚尖方向一致。", "knowledge_ready": True}
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            publish_client = FakeClient(
                [{"domain": "健身", "wiki_title": "深蹲", "summary": "", "knowledge_points": [{"title": "膝盖方向", "content": "膝盖应与脚尖方向一致。", "segment_ids": ["0"]}]}]
            )
            publish_verified(space, Path(copied["path"]), verified, publish_client)
            knowledge_id = load_index(space)[0]["knowledge_id"]
            answer_client = FakeClient([{"answer": "深蹲时膝盖应与脚尖方向一致。", "knowledge_ids": [knowledge_id]}])

            result = answer_question(space, "深蹲膝盖朝哪里", answer_client)

            self.assertEqual(len(result["citations"]), 1)
            self.assertEqual(result["citations"][0]["video_id"], copied["video_id"])

    def test_follow_up_question_uses_bounded_session_context_for_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"
            source = root / "课程.mp4"
            source.write_bytes(b"video")
            copied = copy_video(space, source)
            verified = root / "lesson.verified.json"
            verified.write_text(
                json.dumps(
                    {"segments": [{"id": "0", "start": 1, "end": 4, "final_text": "深蹲时膝盖与脚尖方向一致。", "knowledge_ready": True}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            publish_verified(
                space,
                Path(copied["path"]),
                verified,
                FakeClient([{"domain": "健身", "wiki_title": "深蹲", "summary": "", "knowledge_points": [{"title": "膝盖方向", "content": "膝盖应与脚尖方向一致。", "segment_ids": ["0"]}]}]),
            )
            knowledge_id = load_index(space)[0]["knowledge_id"]
            answer_client = FakeClient([{"answer": "它能帮助膝关节保持正确方向。", "knowledge_ids": [knowledge_id]}])
            conversation = [
                {"role": "user", "content": "深蹲时膝盖方向有什么要求？"},
                {"role": "assistant", "content": "膝盖应与脚尖方向一致。"},
            ]

            result = answer_question(space, "它为什么重要？", answer_client, conversation=conversation)

            self.assertEqual(len(result["citations"]), 1)
            self.assertEqual(answer_client.calls[0]["payload"]["conversation_context"], conversation)
            self.assertIn("只用于理解当前问题中的指代", answer_client.calls[0]["system"])

    def test_space_can_move_and_missing_video_can_be_relinked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_space = root / "space-a"
            source = root / "source.mp4"
            source.write_bytes(b"movable-video")
            copied = copy_video(original_space, source)
            verified = root / "lesson.verified.json"
            verified.write_text(
                json.dumps({"segments": [{"id": "0", "start": 2, "end": 5, "final_text": "移动测试知识。", "knowledge_ready": True}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            publish_verified(
                original_space,
                Path(copied["path"]),
                verified,
                FakeClient([{"domain": "测试", "wiki_title": "移动", "summary": "", "knowledge_points": [{"title": "移动知识", "content": "移动后仍可搜索。", "segment_ids": ["0"]}]}]),
            )
            moved_space = root / "space-b"
            shutil.move(str(original_space), moved_space)
            hits = search_index(moved_space, "移动后搜索")
            self.assertTrue(hits[0]["video_available"])

            moved_video = Path(hits[0]["video_path"])
            external = root / "relocated.mp4"
            shutil.move(str(moved_video), external)
            self.assertFalse(search_index(moved_space, "移动后搜索")[0]["video_available"])
            relinked = relink_video(moved_space, copied["video_id"], external)
            self.assertTrue(Path(relinked["path"]).is_file())
            self.assertTrue(search_index(moved_space, "移动后搜索")[0]["video_available"])

    def test_task_cleanup_only_removes_the_selected_work_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            space = root / "space"
            source = root / "source.mp4"
            source.write_bytes(b"video")
            initialize_space(space)
            task = create_task(space, [source])
            important = space / VIDEO_FOLDER / "keep.mp4"
            important.write_bytes(b"keep")

            self.assertTrue(clean_task_work(space, task["task_id"]))
            self.assertFalse((space / ".work" / "tasks" / task["task_id"]).exists())
            self.assertTrue(important.is_file())


if __name__ == "__main__":
    unittest.main()
