import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WithdrawalDisposalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _participant_with_consent(self, name="Participant One"):
        participant = self.service.create(self.actor, "participant", {"name": name})
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": participant["id"], "scope": ["research"]},
        )
        self.service.transition(
            self.actor,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        return participant, consent

    def _stored_sample(self, participant, consent, code):
        sample = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": participant["id"],
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )
        return self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent["id"]},
        )

    def _loan(self, sample_id):
        return self.service.transition(
            self.actor,
            sample_id,
            "loan",
            {"recipient": "Lab", "purpose": "analysis", "due_at": "2026-12-01"},
        )

    def _withdrawal(self, participant):
        return self.service.create(
            self.actor,
            "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )

    def _approve(self, withdrawal_id, sample_ids):
        return self.service.transition(
            self.actor,
            withdrawal_id,
            "approve",
            {"reason": "participant request", "sample_ids": sample_ids},
        )

    def test_approve_moves_listed_samples_into_disposal_flow(self):
        participant, consent = self._participant_with_consent()
        stored = self._stored_sample(participant, consent, "B-001")
        loaned = self._stored_sample(participant, consent, "B-002")
        self._loan(loaned["id"])
        unlisted = self._stored_sample(participant, consent, "B-003")
        withdrawal = self._withdrawal(participant)

        self._approve(withdrawal["id"], [stored["id"], loaned["id"]])

        self.assertEqual(self.service.get(stored["id"])["status"], "pending_disposal")
        self.assertEqual(self.service.get(loaned["id"])["status"], "pending_recall")
        self.assertEqual(self.service.get(unlisted["id"])["status"], "stored")

    def test_recalled_sample_becomes_pending_disposal_on_return(self):
        participant, consent = self._participant_with_consent()
        loaned = self._stored_sample(participant, consent, "B-002")
        self._loan(loaned["id"])
        withdrawal = self._withdrawal(participant)
        self._approve(withdrawal["id"], [loaned["id"]])
        self.assertEqual(self.service.get(loaned["id"])["status"], "pending_recall")

        returned = self.service.transition(self.actor, loaned["id"], "return", {})

        self.assertEqual(returned["status"], "pending_disposal")

    def test_normal_return_still_goes_back_to_stored(self):
        participant, consent = self._participant_with_consent()
        loaned = self._stored_sample(participant, consent, "B-002")
        self._loan(loaned["id"])

        returned = self.service.transition(self.actor, loaned["id"], "return", {})

        self.assertEqual(returned["status"], "stored")

    def test_loan_and_anonymize_blocked_after_withdrawal_approved(self):
        participant, consent = self._participant_with_consent()
        listed = self._stored_sample(participant, consent, "B-001")
        unlisted = self._stored_sample(participant, consent, "B-003")
        withdrawal = self._withdrawal(participant)
        self._approve(withdrawal["id"], [listed["id"]])

        with self.assertRaises(ValidationError):
            self._loan(unlisted["id"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, unlisted["id"], "anonymize", {"reason": "pipeline"}
            )

    def test_execute_merges_missing_samples_and_disposes_once(self):
        participant, consent = self._participant_with_consent()
        listed_stored = self._stored_sample(participant, consent, "B-001")
        listed_loaned = self._stored_sample(participant, consent, "B-002")
        self._loan(listed_loaned["id"])
        missed_stored = self._stored_sample(participant, consent, "B-003")
        missed_loaned = self._stored_sample(participant, consent, "B-004")
        self._loan(missed_loaned["id"])
        withdrawal = self._withdrawal(participant)
        self._approve(withdrawal["id"], [listed_stored["id"], listed_loaned["id"]])

        executed = self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )

        self.assertEqual(executed["status"], "executed")
        self.assertEqual(
            executed["data"]["disposal_result"],
            {
                "destroyed": sorted([listed_stored["id"], missed_stored["id"]]),
                "pending_recall": sorted([listed_loaned["id"], missed_loaned["id"]]),
            },
        )
        self.assertEqual(self.service.get(listed_stored["id"])["status"], "destroyed")
        self.assertEqual(self.service.get(missed_stored["id"])["status"], "destroyed")
        self.assertEqual(self.service.get(listed_loaned["id"])["status"], "pending_recall")
        self.assertEqual(self.service.get(missed_loaned["id"])["status"], "pending_recall")

    def test_reexecute_returns_same_disposal_result(self):
        participant, consent = self._participant_with_consent()
        stored = self._stored_sample(participant, consent, "B-001")
        withdrawal = self._withdrawal(participant)
        self._approve(withdrawal["id"], [stored["id"]])
        first = self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        sample_after_first = self.service.get(stored["id"])

        second = self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-03"}
        )

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(second["data"], first["data"])
        self.assertEqual(
            second["data"]["disposal_result"], first["data"]["disposal_result"]
        )
        sample_after_second = self.service.get(stored["id"])
        self.assertEqual(sample_after_second["status"], "destroyed")
        self.assertEqual(sample_after_second["version"], sample_after_first["version"])

    def test_pending_disposal_sample_can_be_destroyed(self):
        participant, consent = self._participant_with_consent()
        stored = self._stored_sample(participant, consent, "B-001")
        withdrawal = self._withdrawal(participant)
        self._approve(withdrawal["id"], [stored["id"]])
        self.assertEqual(self.service.get(stored["id"])["status"], "pending_disposal")

        destroyed = self.service.transition(
            self.actor, stored["id"], "destroy", {"reason": "withdrawn consent"}
        )

        self.assertEqual(destroyed["status"], "destroyed")

    def test_approve_rejects_samples_of_other_participants(self):
        participant, consent = self._participant_with_consent()
        other, other_consent = self._participant_with_consent("Participant Two")
        foreign = self._stored_sample(other, other_consent, "B-100")
        withdrawal = self._withdrawal(participant)

        with self.assertRaises(ValidationError):
            self._approve(withdrawal["id"], [foreign["id"]])


if __name__ == "__main__":
    unittest.main()
