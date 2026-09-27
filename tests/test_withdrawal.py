import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WithdrawalCascadeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.participant = self.service.create(
            self.actor, "participant", {"name": "Participant One"}
        )
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": self.participant["id"], "scope": ["research"]},
        )
        self.consent = self.service.transition(
            self.actor,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _stored_sample(self, code):
        sample = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": self.participant["id"],
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )
        return self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": self.consent["id"]},
        )

    def _loan(self, sample_id):
        return self.service.transition(
            self.actor,
            sample_id,
            "loan",
            {"recipient": "Lab", "purpose": "analysis", "due_at": "2026-04-01"},
        )

    def _withdrawal(self):
        return self.service.create(
            self.actor,
            "withdrawal",
            {"participant_id": self.participant["id"], "requested_at": "2026-03-01"},
        )

    def _approve(self, withdrawal_id, sample_ids):
        return self.service.transition(
            self.actor,
            withdrawal_id,
            "approve",
            {"reason": "participant request", "sample_ids": sample_ids},
        )

    def test_approve_marks_listed_samples(self):
        stored = self._stored_sample("B-001")
        loaned = self._loan(self._stored_sample("B-002")["id"])
        withdrawal = self._withdrawal()
        self._approve(withdrawal["id"], [stored["id"], loaned["id"]])
        self.assertEqual(self.service.get(stored["id"])["status"], "pending_disposal")
        self.assertEqual(self.service.get(loaned["id"])["status"], "pending_recall")

    def test_recalled_sample_returned_becomes_pending_disposal(self):
        loaned = self._loan(self._stored_sample("B-010")["id"])
        withdrawal = self._withdrawal()
        self._approve(withdrawal["id"], [loaned["id"]])
        returned = self.service.transition(self.actor, loaned["id"], "return", {})
        self.assertEqual(returned["status"], "pending_disposal")

    def test_normal_return_still_goes_back_to_stored(self):
        loaned = self._loan(self._stored_sample("B-011")["id"])
        returned = self.service.transition(self.actor, loaned["id"], "return", {})
        self.assertEqual(returned["status"], "stored")

    def test_loan_and_anonymize_cannot_bypass_approved_withdrawal(self):
        listed = self._stored_sample("B-020")
        omitted = self._stored_sample("B-021")
        withdrawal = self._withdrawal()
        self._approve(withdrawal["id"], [listed["id"]])
        with self.assertRaises(ConflictError):
            self._loan(omitted["id"])
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.actor, omitted["id"], "anonymize", {"reason": "cleanup"}
            )

    def test_anonymize_remains_allowed_as_disposal(self):
        stored = self._stored_sample("B-030")
        withdrawal = self._withdrawal()
        self._approve(withdrawal["id"], [stored["id"]])
        anonymized = self.service.transition(
            self.actor, stored["id"], "anonymize", {"reason": "disposal"}
        )
        self.assertEqual(anonymized["status"], "anonymized")

    def test_execute_merges_omitted_samples_and_disposes_once(self):
        listed = self._stored_sample("B-040")
        omitted_stored = self._stored_sample("B-041")
        omitted_loaned = self._loan(self._stored_sample("B-042")["id"])
        withdrawal = self._withdrawal()
        self._approve(withdrawal["id"], [listed["id"]])
        executed = self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        disposal = executed["data"]["disposal"]
        self.assertCountEqual(
            disposal["merged_sample_ids"], [omitted_stored["id"], omitted_loaned["id"]]
        )
        sample_ids = [item["sample_id"] for item in disposal["results"]]
        self.assertEqual(len(sample_ids), len(set(sample_ids)))
        self.assertEqual(
            self.service.get(omitted_stored["id"])["status"], "pending_disposal"
        )
        self.assertEqual(
            self.service.get(omitted_loaned["id"])["status"], "pending_recall"
        )
        listed_result = next(
            item for item in disposal["results"] if item["sample_id"] == listed["id"]
        )
        self.assertEqual(listed_result["disposition"], "already_pending")

    def test_reexecute_returns_same_disposal_result(self):
        stored = self._stored_sample("B-050")
        withdrawal = self._withdrawal()
        self._approve(withdrawal["id"], [stored["id"]])
        first = self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        second = self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-03"}
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["data"]["executed_at"], "2026-03-02")
        self.assertEqual(first["data"]["disposal"], second["data"]["disposal"])

    def test_second_withdrawal_does_not_dispose_sample_twice(self):
        stored = self._stored_sample("B-060")
        first = self._withdrawal()
        self._approve(first["id"], [stored["id"]])
        self.service.transition(
            self.actor, first["id"], "execute", {"executed_at": "2026-03-02"}
        )
        second = self._withdrawal()
        self._approve(second["id"], [stored["id"]])
        executed = self.service.transition(
            self.actor, second["id"], "execute", {"executed_at": "2026-03-03"}
        )
        result = next(
            item
            for item in executed["data"]["disposal"]["results"]
            if item["sample_id"] == stored["id"]
        )
        self.assertEqual(result["disposition"], "already_pending")
        self.assertEqual(self.service.get(stored["id"])["status"], "pending_disposal")

    def test_approve_rejects_samples_of_other_participants(self):
        other = self.service.create(
            self.actor, "participant", {"name": "Participant Two"}
        )
        foreign = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": other["id"],
                "sample_code": "X-001",
                "collected_at": "2026-01-01",
            },
        )
        withdrawal = self._withdrawal()
        with self.assertRaises(ValidationError):
            self._approve(withdrawal["id"], [foreign["id"]])


if __name__ == "__main__":
    unittest.main()
