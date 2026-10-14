"""One accepted source-current owner and release-bound lineage assessment."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from .postgres import (release_company, release_company_lineage, release_company_source,
                       source_check_occurrence, source_item, source_item_current,
                       source_observation, source_validity_generation)


def current_observations():
    return select(*source_observation.c).join(source_item_current,
        (source_item_current.c.source_item_id == source_observation.c.source_item_id)
        & (source_item_current.c.source_observation_id == source_observation.c.source_observation_id)).subquery()


def current_observation_id(connection, source_item_id):
    return connection.scalar(select(source_item_current.c.source_observation_id).where(
        source_item_current.c.source_item_id == source_item_id))


def accepted_witnesses(connection, sources, *, required_sources=()):
    """Read the accepted pointer while publication holds the source-item locks."""
    if not set(required_sources).issubset(sources):
        raise ValueError("exact activation omits graph source dependencies")
    rows = connection.execute(select(source_observation.c.source_observation_id,
        source_observation.c.source_item_id, source_item_current.c.source_observation_id.label("current_id"),
        source_item_current.c.occurrence_id).outerjoin(source_item_current,
        source_item_current.c.source_item_id == source_observation.c.source_item_id).where(
        source_observation.c.source_observation_id.in_(sources))).mappings().all()
    result = {}
    for row in rows:
        if row["current_id"] != row["source_observation_id"]:
            raise ValueError("activation witness differs from accepted source revision")
        occurrence = row["occurrence_id"]
        if occurrence is None and row["source_observation_id"] in required_sources:
            raise ValueError("exact activation requires an accepted source-check occurrence")
        result[row["source_observation_id"]] = dict(activation_source_item_id=row["source_item_id"] if occurrence is not None else None,
            accepted_occurrence_id=occurrence)
    if set(result) != set(sources):
        raise ValueError("activation source dependency does not exist")
    return result


@dataclass(frozen=True)
class SourceDeliveryRecord:
    occurrence_id: int
    source_item_id: int
    source_observation_id: int
    job_id: int
    ownership_generation: int
    item_key: str
    checked_at: datetime


def load_delivery(connection, occurrence_id, *, source_item_id, source_observation_id) -> SourceDeliveryRecord:
    for name, value in (("occurrence_id", occurrence_id), ("source_item_id", source_item_id),
                        ("source_observation_id", source_observation_id)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    row = connection.execute(select(source_check_occurrence).join(source_observation,
        (source_observation.c.source_observation_id == source_check_occurrence.c.source_observation_id)
        & (source_observation.c.source_item_id == source_check_occurrence.c.source_item_id)).where(
        source_check_occurrence.c.occurrence_id == occurrence_id,
        source_check_occurrence.c.source_item_id == source_item_id,
        source_check_occurrence.c.source_observation_id == source_observation_id)).mappings().one_or_none()
    if row is None:
        raise ValueError("source delivery does not match retained occurrence/item/observation")
    return SourceDeliveryRecord(**row)


def record_check(connection, lease, *, source_item_id, source_observation_id, item_key, checked_at) -> SourceDeliveryRecord:
    """Called only inside the source repository's locked live-lease transaction."""
    connection.execute(select(source_item.c.source_item_id).where(
        source_item.c.source_item_id == source_item_id).with_for_update()).scalar_one()
    values = dict(source_item_id=source_item_id, source_observation_id=source_observation_id,
                  job_id=lease.job_id, ownership_generation=lease.generation,
                  item_key=item_key, checked_at=checked_at)
    occurrence = connection.execute(insert(source_check_occurrence).values(**values)
        .on_conflict_do_nothing(constraint="uq_source_check_delivery")
        .returning(source_check_occurrence)).mappings().one_or_none()
    if occurrence is None:
        # A replay can finish its job outcome without moving current state backward.
        existing = connection.execute(select(source_check_occurrence.c.occurrence_id).where(
            source_check_occurrence.c.job_id == lease.job_id,
            source_check_occurrence.c.ownership_generation == lease.generation,
            source_check_occurrence.c.item_key == item_key,
            source_check_occurrence.c.source_observation_id == source_observation_id)).scalar_one()
        return load_delivery(connection, existing, source_item_id=source_item_id, source_observation_id=source_observation_id)
    current = connection.execute(select(source_item_current).where(
        source_item_current.c.source_item_id == source_item_id)).mappings().one_or_none()
    if current:
        ordering_time = current["checked_at"]
        if ordering_time is None:
            ordering_time = connection.scalar(select(source_observation.c.detected_at).where(
                source_observation.c.source_observation_id == current["source_observation_id"]))
        if checked_at < ordering_time:
            return SourceDeliveryRecord(**occurrence)
    pointer = dict(source_item_id=source_item_id, source_observation_id=source_observation_id,
                   occurrence_id=occurrence["occurrence_id"], checked_at=checked_at)
    connection.execute(insert(source_item_current).values(**pointer).on_conflict_do_update(
        constraint="pk_source_item_current", set_={key: value for key, value in pointer.items() if key != "source_item_id"}))
    if current is None or current["source_observation_id"] != source_observation_id or current["occurrence_id"] is None:
        # ponytail: one generation row serializes changes; use per-source generations if contention is measured.
        connection.execute(update(source_validity_generation).where(source_validity_generation.c.singleton_id == 1)
            .values(generation=source_validity_generation.c.generation + 1))
    return SourceDeliveryRecord(**occurrence)


def insert_release_lineage(connection, release_id, memberships, *, parent_release_id=None,
                           changed_sources=None, display_members=None, activation_witnesses=None):
    changed_sources = changed_sources or {}
    activation_witnesses = activation_witnesses or {}
    displayed = {entry["security_id"]: tuple(entry["source_observation_ids"]) for entry in (display_members or ())}
    inherited = {}
    if parent_release_id is not None:
        for row in connection.execute(select(release_company_lineage).where(
            release_company_lineage.c.release_id == parent_release_id)).mappings():
            inherited[row["security_id"]] = (row["assessment"], [])
        for row in connection.execute(select(release_company_source).where(
            release_company_source.c.release_id == parent_release_id)).mappings():
            inherited[row["security_id"]][1].append(dict(source_observation_id=row["source_observation_id"],
                activation_source_item_id=row["activation_source_item_id"], accepted_occurrence_id=row["accepted_occurrence_id"]))
    for member in memberships:
        security = member["security_id"]
        if security in displayed:
            sources = [dict(source_observation_id=value, **activation_witnesses.get(security, {}).get(value, {})) for value in displayed[security]]
            assessment = "COMPLETE" if sources else "NOT_ASSESSED"
        elif security in changed_sources:
            sources, assessment = [dict(source_observation_id=value, **activation_witnesses.get(security, {}).get(value, {})) for value in changed_sources[security]], "COMPLETE"
        else:
            assessment, sources = inherited.get(security, ("NOT_ASSESSED", ()))
        if assessment == "COMPLETE" and not sources:
            raise ValueError("complete release lineage requires source dependencies")
        connection.execute(release_company_lineage.insert().values(
            release_id=release_id, security_id=security, assessment=assessment))
        if sources:
            connection.execute(release_company_source.insert(), [dict(release_id=release_id,
                security_id=security, **value) for value in sources])


def dependency_status(bound_id, current_id, state, metadata, occurrence_id):
    reason = metadata.get("reason")
    if current_id is None or occurrence_id is None:
        return "NOT_ASSESSED"
    if state == "removed":
        return "WITHDRAWN" if reason == "withdrawn" else "SOURCE_REMOVED_REVIEW_REQUIRED"
    if state == "pending":
        return "INPUTS_PENDING"
    if state == "unavailable":
        return "SOURCE_ACCESS_UNAVAILABLE"
    if state == "present" and bound_id == current_id:
        return "CAPTURED_MATCH"
    return "REVALIDATION_REQUIRED"


class SourceValidityRepository:
    def __init__(self, engine):
        self.engine = engine

    def assess(self, release_id, security_ids):
        if type(release_id) is not int or release_id <= 0:
            raise ValueError("release_id must be positive")
        ids = tuple(security_ids)
        if not ids or len(ids) > 300 or any(type(value) is not int or value <= 0 for value in ids):
            raise ValueError("assessment requires 1 to 300 positive security IDs")
        # All rows and the generation must share a DB snapshot across source commits.
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                return self.assess_on_connection(connection, release_id, ids)

    @staticmethod
    def assess_on_connection(connection, release_id, security_ids):
        members = connection.execute(select(release_company.c.security_id, release_company_lineage.c.assessment)
            .outerjoin(release_company_lineage,
                (release_company_lineage.c.release_id == release_company.c.release_id)
                & (release_company_lineage.c.security_id == release_company.c.security_id))
            .where(release_company.c.release_id == release_id, release_company.c.security_id.in_(security_ids))).mappings().all()
        generation = connection.scalar(select(source_validity_generation.c.generation).where(source_validity_generation.c.singleton_id == 1))
        current = current_observations()
        dependencies = connection.execute(select(release_company_source.c.security_id,
            release_company_source.c.source_observation_id.label("bound_observation_id"),
            source_observation.c.source_item_id, current.c.source_observation_id.label("current_observation_id"),
            current.c.state, current.c.canonical_metadata, source_item_current.c.occurrence_id,
            source_item_current.c.checked_at)
            .join(source_observation, source_observation.c.source_observation_id == release_company_source.c.source_observation_id)
            .outerjoin(current, current.c.source_item_id == source_observation.c.source_item_id)
            .outerjoin(source_item_current, source_item_current.c.source_item_id == source_observation.c.source_item_id)
            .where(release_company_source.c.release_id == release_id, release_company_source.c.security_id.in_(security_ids))
            .order_by(release_company_source.c.security_id, source_observation.c.source_item_id)).mappings().all()
        by_security = {member["security_id"]: {"security_id": member["security_id"],
            "lineage_assessment": member["assessment"] or "NOT_ASSESSED", "dependencies": []} for member in members}
        for row in dependencies:
            value = dict(row)
            value["status"] = dependency_status(row["bound_observation_id"], row["current_observation_id"],
                row["state"], row["canonical_metadata"] or {}, row["occurrence_id"])
            value["reason"] = (row["canonical_metadata"] or {}).get("reason")
            del value["canonical_metadata"]
            by_security[row["security_id"]]["dependencies"].append(value)
        for member in by_security.values():
            statuses = {row["status"] for row in member["dependencies"]}
            member["status"] = "NOT_ASSESSED" if member["lineage_assessment"] != "COMPLETE" or not statuses else (
                "CAPTURED_MATCH" if statuses == {"CAPTURED_MATCH"} else "REVALIDATION_REQUIRED")
            member["source_statuses"] = sorted(statuses)
        return {"release_id": release_id, "validity_generation": generation, "companies": list(by_security.values()),
                "freshness_target": None, "numeric_invalidation_assessed": False}
