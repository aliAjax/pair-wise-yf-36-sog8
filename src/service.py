from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "withdrawal" and action == "approve":
            return self._approve_withdrawal(actor, entity, dict(data or {}), expected_version)
        if kind == "withdrawal" and action == "execute":
            return self._execute_withdrawal(actor, entity, dict(data or {}), expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _approve_withdrawal(self, actor, entity, data, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, "approve", data, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updates = [(entity["id"], expected, next_status, merged)]
        cascaded = []
        for sample_id in patch.get("sample_ids") or []:
            sample = self.repository.get_entity(sample_id)
            if not sample:
                continue
            target = self.rules.withdrawal_sample_target(sample["status"])
            if not target:
                continue
            sample_data = dict(sample["data"])
            sample_data["marked_by_withdrawal"] = entity["id"]
            updates.append((sample["id"], sample["version"], target, sample_data))
            cascaded.append((sample, target))
        self.repository.update_entities(updates)
        self.audit.record(
            entity["id"], actor, "approve", entity["status"], next_status, {"patch": patch}
        )
        self._record_cascade(actor, entity["id"], cascaded)
        return self.repository.get_entity(entity["id"])

    def _execute_withdrawal(self, actor, entity, data, expected_version):
        self.rules.ensure_action_role(actor, "withdrawal", "execute")
        if entity["status"] == "executed":
            return entity
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, "execute", data, self._lookup
        )
        participant_id = entity["data"].get("participant_id")
        listed = list(entity["data"].get("sample_ids") or [])
        owned = self._lookup("sample", "participant_id", participant_id) or []
        merged_ids = list(dict.fromkeys(listed + [sample["id"] for sample in owned]))
        omitted = [sample_id for sample_id in merged_ids if sample_id not in listed]
        results = []
        updates = []
        cascaded = []
        for sample_id in merged_ids:
            sample = self.repository.get_entity(sample_id)
            if not sample or sample["data"].get("participant_id") != participant_id:
                results.append(
                    {"sample_id": sample_id, "disposition": "skipped",
                     "reason": "unknown or foreign sample"}
                )
                continue
            status = sample["status"]
            target = self.rules.withdrawal_sample_target(status)
            if target:
                sample_data = dict(sample["data"])
                sample_data["marked_by_withdrawal"] = entity["id"]
                updates.append((sample["id"], sample["version"], target, sample_data))
                cascaded.append((sample, target))
                results.append(
                    {"sample_id": sample_id, "from_status": status,
                     "to_status": target, "disposition": "marked"}
                )
            elif status in ("pending_disposal", "pending_recall"):
                results.append(
                    {"sample_id": sample_id, "from_status": status,
                     "to_status": status, "disposition": "already_pending"}
                )
            else:
                results.append(
                    {"sample_id": sample_id, "from_status": status,
                     "to_status": status, "disposition": "not_actionable"}
                )
        disposal = {
            "participant_id": participant_id,
            "executed_at": patch.get("executed_at"),
            "listed_sample_ids": listed,
            "merged_sample_ids": omitted,
            "results": results,
        }
        merged = dict(entity["data"])
        merged.update(patch)
        merged["disposal"] = disposal
        updates.insert(0, (entity["id"], expected, next_status, merged))
        self.repository.update_entities(updates)
        self.audit.record(
            entity["id"], actor, "execute", entity["status"], next_status,
            {"patch": patch, "disposal": disposal},
        )
        self._record_cascade(actor, entity["id"], cascaded)
        return self.repository.get_entity(entity["id"])

    def _record_cascade(self, actor, withdrawal_id, cascaded):
        for sample, target in cascaded:
            self.audit.record(
                sample["id"],
                actor,
                "withdrawal_cascade",
                sample["status"],
                target,
                {"withdrawal_id": withdrawal_id},
            )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
