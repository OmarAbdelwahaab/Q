from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from pipeline.config import QAGateSettings
from pipeline.qa_gate.service import QAGateService, RenderTrigger
from pipeline.state.repository import PipelineStateRepository


class RecordingAlerts:
    def __init__(self) -> None:
        self.holds: list[tuple[str, dict[str, object] | None]] = []
        self.failures: list[str] = []

    async def send_qa_gate_hold(
        self, message: str, payload: dict[str, object] | None = None
    ) -> None:
        self.holds.append((message, payload))

    async def send_qa_gate_failure(self, message: str) -> None:
        self.failures.append(message)


class RecordingRenderTrigger:
    def __init__(self) -> None:
        self.triggered_ids: list[int] = []

    async def trigger_render(self, message_id: int) -> None:
        self.triggered_ids.append(message_id)


def write_match_artifact(
    storage_root: Path,
    message_id: int,
    confidence: float = 0.95,
    surah: int = 1,
    ayah_start: int = 1,
    ayah_end: int = 2,
    canonical_text: str = "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ",
    recognized_text: str = "بسم الله الرحمن الرحيم الحمد لله رب العالمين",
) -> Path:
    destination = storage_root / "match" / f"{message_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "surah": surah,
        "ayah_start": ayah_start,
        "ayah_end": ayah_end,
        "canonical_text": canonical_text,
        "match_confidence": confidence,
        "recognized_text": recognized_text,
    }
    destination.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return destination


def write_align_artifact(
    storage_root: Path,
    message_id: int,
    coverage: float = 0.98,
    words: list[dict[str, object]] | None = None,
) -> Path:
    destination = storage_root / "align" / f"{message_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "words": words or [{"word": "بسم", "start_ms": 0, "end_ms": 400}],
        "alignment_coverage": coverage,
    }
    destination.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return destination


class QAGateTests(unittest.TestCase):
    def test_qa_gate_passes_when_scores_meet_or_exceed_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_match_artifact(root, 601, confidence=0.95)
            write_align_artifact(root, 601, coverage=0.98)
            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                match_confidence_threshold=0.90,
                alignment_coverage_threshold=0.95,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(601))
            self.assertEqual(result.status, "approved")
            self.assertTrue(result.approved)
            self.assertEqual(result.match_confidence, 0.95)
            self.assertEqual(result.alignment_coverage, 0.98)
            self.assertIsNone(result.error)

            # Assert state row
            stage_row = state.fetch_stage(601, "qa_gate")
            self.assertIsNotNone(stage_row)
            self.assertEqual(stage_row["status"], "approved")
            self.assertIsNone(stage_row["error"])

            # Assert render trigger was invoked
            self.assertEqual(render.triggered_ids, [601])

            # Assert no review queue file was created
            self.assertFalse((root / "review_queue" / "601.json").exists())

            # Assert no alerts were sent
            self.assertEqual(len(alerts.holds), 0)
            self.assertEqual(len(alerts.failures), 0)

    def test_qa_gate_rejects_on_low_match_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_match_artifact(root, 602, confidence=0.85)
            write_align_artifact(root, 602, coverage=0.98)
            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                match_confidence_threshold=0.90,
                alignment_coverage_threshold=0.95,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(602))
            self.assertEqual(result.status, "rejected")
            self.assertFalse(result.approved)
            self.assertIn("match_confidence 0.8500 < 0.9000", result.error or "")

            # Assert state row
            stage_row = state.fetch_stage(602, "qa_gate")
            self.assertIsNotNone(stage_row)
            self.assertEqual(stage_row["status"], "rejected")
            self.assertIn("QA threshold check failed", stage_row["error"])

            # Assert render was NEVER triggered
            self.assertEqual(render.triggered_ids, [])

            # Assert review queue file exists and has full diagnostic data
            review_file = root / "review_queue" / "602.json"
            self.assertTrue(review_file.exists())
            data = json.loads(review_file.read_text(encoding="utf-8"))
            self.assertEqual(data["message_id"], 602)
            self.assertEqual(data["status"], "held_for_review")
            self.assertEqual(data["match_confidence"], 0.85)
            self.assertEqual(data["match_confidence_threshold"], 0.90)
            self.assertEqual(data["alignment_coverage"], 0.98)
            self.assertIn("بِسْمِ اللَّهِ", data["canonical_text"])
            self.assertIn("بسم الله", data["recognized_text"])

            # Assert alert was dispatched with both texts and scores
            self.assertEqual(len(alerts.holds), 1)
            alert_msg, payload = alerts.holds[0]
            self.assertIn("602", alert_msg)
            self.assertIn("0.8500", alert_msg)
            self.assertIn("بِسْمِ اللَّهِ", alert_msg)
            self.assertIn("بسم الله", alert_msg)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["message_id"], 602)

    def test_qa_gate_rejects_on_low_alignment_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_match_artifact(root, 603, confidence=0.96)
            write_align_artifact(root, 603, coverage=0.88)
            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                match_confidence_threshold=0.90,
                alignment_coverage_threshold=0.95,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(603))
            self.assertEqual(result.status, "rejected")
            self.assertFalse(result.approved)
            self.assertIn("alignment_coverage 0.8800 < 0.9500", result.error or "")

            # Render must not be triggered
            self.assertEqual(render.triggered_ids, [])

            # Review queue item must be created
            review_file = root / "review_queue" / "603.json"
            self.assertTrue(review_file.exists())
            data = json.loads(review_file.read_text(encoding="utf-8"))
            self.assertEqual(data["alignment_coverage"], 0.88)

            # Alert dispatched
            self.assertEqual(len(alerts.holds), 1)
            self.assertIn("0.8800", alerts.holds[0][0])

    def test_qa_gate_rejects_when_both_scores_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_match_artifact(root, 604, confidence=0.75)
            write_align_artifact(root, 604, coverage=0.80)
            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                match_confidence_threshold=0.90,
                alignment_coverage_threshold=0.95,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(604))
            self.assertEqual(result.status, "rejected")
            self.assertFalse(result.approved)
            self.assertEqual(len(result.reasons), 2)
            self.assertIn("match_confidence", result.reasons[0])
            self.assertIn("alignment_coverage", result.reasons[1])
            self.assertEqual(render.triggered_ids, [])

    def test_qa_gate_boundary_precision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()
            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                match_confidence_threshold=0.90,
                alignment_coverage_threshold=0.95,
                render_trigger=render,
            )

            # Exactly equal to threshold -> PASS
            write_match_artifact(root, 605, confidence=0.90)
            write_align_artifact(root, 605, coverage=0.95)
            result = asyncio.run(service.evaluate(605))
            self.assertEqual(result.status, "approved")
            self.assertTrue(result.approved)

            # 0.8999 vs 0.9000 -> FAIL
            write_match_artifact(root, 606, confidence=0.8999)
            write_align_artifact(root, 606, coverage=0.95)
            result2 = asyncio.run(service.evaluate(606))
            self.assertEqual(result2.status, "rejected")
            self.assertFalse(result2.approved)

    def test_qa_gate_missing_match_artifact_fails_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_align_artifact(root, 607, coverage=0.98)
            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(607))
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.approved)
            self.assertIn("Match artifact missing", result.error or "")

            stage_row = state.fetch_stage(607, "qa_gate")
            self.assertEqual(stage_row["status"], "failed")
            self.assertEqual(len(alerts.failures), 1)
            self.assertEqual(render.triggered_ids, [])
            self.assertFalse((root / "review_queue" / "607.json").exists())

    def test_qa_gate_missing_align_artifact_fails_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_match_artifact(root, 608, confidence=0.95)
            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(608))
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.approved)
            self.assertIn("Alignment artifact missing", result.error or "")

            stage_row = state.fetch_stage(608, "qa_gate")
            self.assertEqual(stage_row["status"], "failed")
            self.assertEqual(len(alerts.failures), 1)
            self.assertEqual(render.triggered_ids, [])

    def test_qa_gate_corrupted_json_fails_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            match_file = root / "match" / "609.json"
            match_file.parent.mkdir(parents=True)
            match_file.write_text("{not valid json", encoding="utf-8")
            write_align_artifact(root, 609, coverage=0.98)

            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(609))
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.approved)
            self.assertIn("Malformed artifact data", result.error or "")

            stage_row = state.fetch_stage(609, "qa_gate")
            self.assertEqual(stage_row["status"], "failed")
            self.assertEqual(len(alerts.failures), 1)
            self.assertEqual(render.triggered_ids, [])

    def test_qa_gate_custom_thresholds_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Match 0.82, coverage 0.85
            write_match_artifact(root, 610, confidence=0.82)
            write_align_artifact(root, 610, coverage=0.85)

            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            # With relaxed thresholds, this should pass
            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                match_confidence_threshold=0.80,
                alignment_coverage_threshold=0.85,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(610))
            self.assertEqual(result.status, "approved")
            self.assertTrue(result.approved)
            self.assertEqual(render.triggered_ids, [610])

    def test_qa_gate_settings_from_env(self) -> None:
        orig_match = os.environ.get("QA_MATCH_CONFIDENCE_THRESHOLD")
        orig_align = os.environ.get("QA_ALIGNMENT_COVERAGE_THRESHOLD")
        try:
            os.environ["QA_MATCH_CONFIDENCE_THRESHOLD"] = "0.88"
            os.environ["QA_ALIGNMENT_COVERAGE_THRESHOLD"] = "0.92"
            settings = QAGateSettings.from_env()
            self.assertEqual(settings.match_confidence_threshold, 0.88)
            self.assertEqual(settings.alignment_coverage_threshold, 0.92)
        finally:
            if orig_match is not None:
                os.environ["QA_MATCH_CONFIDENCE_THRESHOLD"] = orig_match
            else:
                os.environ.pop("QA_MATCH_CONFIDENCE_THRESHOLD", None)
            if orig_align is not None:
                os.environ["QA_ALIGNMENT_COVERAGE_THRESHOLD"] = orig_align
            else:
                os.environ.pop("QA_ALIGNMENT_COVERAGE_THRESHOLD", None)

    def test_qa_gate_settings_invalid_threshold_raises(self) -> None:
        orig = os.environ.get("QA_MATCH_CONFIDENCE_THRESHOLD")
        try:
            os.environ["QA_MATCH_CONFIDENCE_THRESHOLD"] = "1.5"
            with self.assertRaises(ValueError):
                QAGateSettings.from_env()
        finally:
            if orig is not None:
                os.environ["QA_MATCH_CONFIDENCE_THRESHOLD"] = orig
            else:
                os.environ.pop("QA_MATCH_CONFIDENCE_THRESHOLD", None)

    def test_end_to_end_pipeline_flow_through_qa_gate_approved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            # Record prior pipeline stages
            state.upsert_stage(701, "ingestion", "completed")
            state.upsert_stage(701, "audio", "completed")
            state.upsert_stage(701, "recognition", "completed")
            state.upsert_stage(701, "alignment", "completed")

            write_match_artifact(root, 701, confidence=0.96)
            write_align_artifact(root, 701, coverage=0.97)

            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                match_confidence_threshold=0.90,
                alignment_coverage_threshold=0.95,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(701))
            self.assertEqual(result.status, "approved")
            self.assertTrue(result.approved)
            self.assertEqual(render.triggered_ids, [701])
            self.assertEqual(state.fetch_stage(701, "qa_gate")["status"], "approved")

    def test_end_to_end_pipeline_flow_rejected_never_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = PipelineStateRepository(root / "state.db")
            alerts = RecordingAlerts()
            render = RecordingRenderTrigger()

            # Record prior pipeline stages
            state.upsert_stage(702, "ingestion", "completed")
            state.upsert_stage(702, "audio", "completed")
            state.upsert_stage(702, "recognition", "completed")
            state.upsert_stage(702, "alignment", "completed")

            write_match_artifact(root, 702, confidence=0.82)
            write_align_artifact(root, 702, coverage=0.91)

            service = QAGateService(
                storage_root=root,
                state_repository=state,
                alert_service=alerts,
                match_confidence_threshold=0.90,
                alignment_coverage_threshold=0.95,
                render_trigger=render,
            )

            result = asyncio.run(service.evaluate(702))
            self.assertEqual(result.status, "rejected")
            self.assertFalse(result.approved)

            # Guarantees:
            # 1. Render is NEVER triggered
            self.assertEqual(render.triggered_ids, [])
            # 2. State in database is rejected, not approved or completed
            self.assertEqual(state.fetch_stage(702, "qa_gate")["status"], "rejected")
            # 3. Item is safely held in review_queue
            self.assertTrue((root / "review_queue" / "702.json").exists())
            # 4. Detailed alert was emitted
            self.assertEqual(len(alerts.holds), 1)
