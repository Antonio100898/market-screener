"""Ordered captured input associations; resolver only, never financial assembly."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, StrictInt, TypeAdapter, model_validator

from . import (artifacts as artifact_owner, captured_assembly, captured_metadata,
    complete_accounting, durable_jobs, figure_history, job_runtime, object_store,
    profiles, publication, sectors, shared_companies, source_observations, source_validity)
from .artifacts import read_registered_artifact, store_evidence
from .captured_metadata import SourceRef, SubmissionsRef, resolve_submissions, verify_source
from .complete_accounting import Identity, Sha, Text, _Record, load_accounting
from .durable_jobs import JobDeferred
from .sources.sec_json import decode_object

MAX_ENVELOPE_BYTES = 1024 * 1024
MAX_PLAN_BYTES = 64 * 1024 * 1024
MAX_PREPARATION_BYTES = 8 * 1024 * 1024
PLAN_MEDIA_TYPE = "application/vnd.market-screener.complete-input-plan+json"
REQUIRED_GAPS = frozenset({"SOURCE_AUTHORITY_NOT_ASSESSED", "LISTING_INDEX_NOT_RESOLVED",
    "EDINET_METADATA_NOT_RESOLVED", "PRIOR_ACCEPTED_STATE_NOT_CAPTURED"})
RESOLUTION_MEDIA_TYPE = "application/vnd.market-screener.complete-input-resolution+json"


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def load(model, raw):
    # Reuse strict duplicate-key/nonfinite admission; JSON mode admits dates and tuples.
    return model.model_validate_json(encode(decode_object(raw, "complete captured inputs")))


class Issuer(_Record):
    source: Literal["SEC", "EDINET"]
    identifier: Text


class CanonicalRef(_Record):
    recipe_sha256: Sha
    preparation_sha256: Sha
    member_ordinal: StrictInt = Field(ge=0)


class BundleRef(_Record):
    exact_bundle_id: Identity
    exact_bundle_sha256: Sha
    expected_exact_run_key: Text


class UnavailableAssociation(_Record):
    status: Literal["UNAVAILABLE", "REGISTERED_ARTIFACT_ONLY"]
    artifact_sha256: Sha | None
    reason: Text

    @model_validator(mode="after")
    def status_binding(self):
        if (self.artifact_sha256 is not None) != (self.status == "REGISTERED_ARTIFACT_ONLY"):
            raise ValueError("unavailable association cannot invent a retained artifact")
        return self


class IndexAssociation(UnavailableAssociation):
    name: Literal["S&P 500", "DJIA", "Nasdaq 100"]


class ParentState(UnavailableAssociation):
    parent_release_id: Identity
    security_id: Identity | None
    snapshot_id: Identity | None


class InputRecord(_Record):
    ordinal: StrictInt = Field(ge=0)
    input_key: Text
    issuer: Issuer
    sources: tuple[SourceRef, ...]
    canonical: CanonicalRef | None
    exact_bundle: BundleRef | None
    submissions: SubmissionsRef | None
    parent_state: ParentState | None
    gaps: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def associations(self):
        deliveries = [source.delivery.occurrence_id for source in self.sources]
        if len(set(deliveries)) != len(deliveries):
            raise ValueError("duplicate delivery in input record")
        if self.submissions is not None and self.submissions.source not in self.sources:
            raise ValueError("metadata source must belong to the pinned input deliveries")
        if self.exact_bundle is not None and self.canonical is None:
            raise ValueError("exact bundle requires canonical member")
        return self


class Chunk(_Record):
    contract: Literal["COMPLETE_INPUT_CHUNK1"]
    records: tuple[InputRecord, ...] = Field(min_length=1)


class ChunkRef(_Record):
    ordinal: StrictInt = Field(ge=0)
    start: StrictInt = Field(ge=0)
    count: Identity
    byte_size: Identity = Field(le=MAX_ENVELOPE_BYTES)
    artifact_sha256: Sha


class PreparationRef(_Record):
    recipe_sha256: Sha
    preparation_sha256: Sha


class CompleteInputPlan(_Record):
    contract: Literal["COMPLETE_INPUT_PLAN1"]
    accounting_sha256: Sha
    resolver_build_sha256: Sha
    canonical_engine_sha256: Sha
    operation_at: AwareDatetime
    export_mode: Literal["FULL", "QUOTE_ONLY", "NO_PRICES"]
    global_listing: UnavailableAssociation
    global_indexes: tuple[IndexAssociation, ...] = Field(min_length=1)
    chunks: tuple[ChunkRef, ...] = Field(min_length=1)
    preparations: tuple[PreparationRef, ...]
    gaps: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ranges(self):
        start = 0
        for ordinal, chunk in enumerate(self.chunks):
            if chunk.ordinal != ordinal or chunk.start != start:
                raise ValueError("chunk ranges must be contiguous and ordered")
            start += chunk.count
        if {index.name for index in self.global_indexes} != {"S&P 500", "DJIA", "Nasdaq 100"} or len(self.global_indexes) != 3:
            raise ValueError("all existing global index associations require explicit coverage")
        if not REQUIRED_GAPS.issubset(self.gaps):
            raise ValueError("complete plan omits unresolved owner coverage")
        identities = [(p.recipe_sha256, p.preparation_sha256) for p in self.preparations]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate preparation association")
        return self


class ResolvedInput(_Record):
    input: InputRecord
    metadata_sha256: Sha | None


class Resolution(_Record):
    contract: Literal["COMPLETE_INPUT_RESOLUTION1"]
    complete_plan_sha256: Sha
    accounting_sha256: Sha
    resolver_build_sha256: Sha
    canonical_engine_sha256: Sha
    scope: Literal["RESOLVER_ONLY"]
    financial_status: Literal["NOT_ASSESSED"]
    display_status: Literal["NOT_BUILT"]
    engine_identity_status: Literal["DECLARED_NOT_VERIFIED"]
    inputs: tuple[ResolvedInput, ...]
    gaps: tuple[Text, ...]


_HELPER_MODULES = (artifact_owner, captured_metadata, complete_accounting, durable_jobs,
    figure_history, job_runtime, object_store, profiles, publication, sectors, shared_companies,
    source_observations, source_validity)
_HELPER_FILES = [Path(m.__file__) for m in _HELPER_MODULES] + [Path(__file__)]
_IMPORT_HELPERS = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in _HELPER_FILES}
_IMPORT_CANONICAL = captured_assembly.engine_identity()


def resolver_identity():
    helpers = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in _HELPER_FILES}
    canonical = captured_assembly.engine_identity()
    if helpers != _IMPORT_HELPERS or canonical != _IMPORT_CANONICAL:
        raise JobDeferred("resolver source changed since import; restart a fresh frozen process")
    base = Path(__file__).parent
    return hashlib.sha256(encode(dict(canonical_engine_sha256=canonical,
        helpers={str(Path(name).relative_to(base)): digest for name, digest in helpers.items()}))).hexdigest()


def pack_chunks(records):
    """Pack actual UTF-8 bytes; one oversized record is an explicit refusal."""
    prefix, suffix = b'{"contract":"COMPLETE_INPUT_CHUNK1","records":[', b']}'
    pending, size, total = [], len(prefix) + len(suffix), 0
    for record in records:
        raw_record = encode(record.model_dump(mode="json"))
        load(InputRecord, raw_record)
        if len(prefix) + len(raw_record) + len(suffix) > MAX_ENVELOPE_BYTES:
            raise ValueError("input record exceeds1MiB chunk ceiling")
        needed = len(raw_record) + bool(pending)
        if size + needed > MAX_ENVELOPE_BYTES:
            raw = prefix + b','.join(pending) + suffix
            total += len(raw)
            if total > MAX_PLAN_BYTES:
                raise ValueError("chunk envelopes exceed64MiB")
            yield raw
            pending, size, needed = [], len(prefix) + len(suffix), len(raw_record)
        pending.append(raw_record)
        size += needed
    if pending:
        raw = prefix + b','.join(pending) + suffix
        if total + len(raw) > MAX_PLAN_BYTES:
            raise ValueError("chunk envelopes exceed64MiB")
        yield raw


class CompleteInputResolver:
    def __init__(self, canonical, artifacts, objects, observations, companies, history, publications):
        self.canonical, self.artifacts, self.objects = canonical, artifacts, objects
        self.observations, self.companies, self.history = observations, companies, history
        self.publications = publications

    def _read(self, digest, maximum, payloads):
        return read_registered_artifact(digest, self.objects, self.artifacts,
            maximum=maximum, label="complete captured inputs", verified_payloads=payloads)[1]

    def _store(self, value):
        raw = encode(value)
        if len(raw) > MAX_PREPARATION_BYTES:
            raise ValueError("captured metadata output exceeds8MiB")
        return store_evidence(raw, RESOLUTION_MEDIA_TYPE, self.objects, self.artifacts).content_sha256

    def record_plan(self, *, accounting_sha256, records, preparations, operation_at, export_mode,
                    global_listing, global_indexes, gaps):
        """Producer takes completed owner references and explicit unsuccessful evidence."""
        refs, start = [], 0
        for ordinal, raw in enumerate(pack_chunks(records)):
            chunk = load(Chunk, raw)
            artifact = store_evidence(raw, PLAN_MEDIA_TYPE, self.objects, self.artifacts)
            refs.append(dict(ordinal=ordinal, start=start, count=len(chunk.records),
                             byte_size=len(raw), artifact_sha256=artifact.content_sha256))
            start += len(chunk.records)
        plan = load(CompleteInputPlan, encode(dict(contract="COMPLETE_INPUT_PLAN1",
            accounting_sha256=accounting_sha256, resolver_build_sha256=resolver_identity(),
            canonical_engine_sha256=captured_assembly.engine_identity(), operation_at=operation_at,
            export_mode=export_mode, global_listing=global_listing, global_indexes=global_indexes, chunks=refs, preparations=preparations, gaps=sorted(set(gaps) | REQUIRED_GAPS))))
        raw = encode(plan.model_dump(mode="json"))
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError("complete root exceeds1MiB")
        # Validate actual completed owner references before retaining the root.
        self._inputs(plan, raw, {})
        return store_evidence(raw, PLAN_MEDIA_TYPE, self.objects, self.artifacts).content_sha256

    def _inputs(self, plan, raw, payloads):
        build = resolver_identity()
        if plan.resolver_build_sha256 != build or plan.canonical_engine_sha256 != captured_assembly.engine_identity():
            raise JobDeferred("complete resolver or canonical build differs")
        accounting_budget = MAX_PLAN_BYTES - len(raw) - sum(ref.byte_size for ref in plan.chunks)
        if accounting_budget < 0:
            raise ValueError("complete plan envelopes exceed64MiB")
        accounting_raw = self._read(plan.accounting_sha256, accounting_budget, {})
        accounting = load_accounting(accounting_raw)
        if accounting.engine_identity_sha256 != plan.canonical_engine_sha256:
            raise ValueError("accounting build declaration differs from plan")
        total = len(raw) + len(accounting_raw)
        for association in (plan.global_listing, *plan.global_indexes):
            if association.artifact_sha256 is not None:
                self._read(association.artifact_sha256, MAX_PLAN_BYTES, {})
        records = []
        for ref in plan.chunks:
            body = self._read(ref.artifact_sha256, MAX_ENVELOPE_BYTES, {})
            total += len(body)
            if total > MAX_PLAN_BYTES:
                raise ValueError("complete plan envelopes exceed64MiB")
            chunk = load(Chunk, body)
            if len(body) != ref.byte_size or len(chunk.records) != ref.count:
                raise ValueError("chunk bytes/count differ from root")
            if [r.ordinal for r in chunk.records] != list(range(ref.start, ref.start + ref.count)):
                raise ValueError("input ordinals differ from chunk range")
            records.extend(chunk.records)
        if [r.input_key for r in records] != [r.key for r in accounting.universe]:
            raise ValueError("ordered complete inputs differ from accounting universe")
        inputs = {i.key: set(i.artifact_sha256s) for i in accounting.universe}
        # Admit source/item/delivery/body identities before canonical or metadata parsing.
        for record in records:
            allowed = inputs[record.input_key]
            # No cohort cache of Company Facts/native/market bodies. A preparation
            # owns its temporary financial reads; individual input reads are released.
            for digest in allowed:
                self._read(digest, MAX_PLAN_BYTES, {})
            for source in record.sources:
                verify_source(source, record.issuer.source, record.issuer.identifier, self.observations)
                if source.artifact_sha256 is not None:
                    if source.artifact_sha256 not in allowed:
                        raise ValueError("source response omitted from retained input accounting")
                    artifact = self.artifacts.get(source.artifact_sha256)
                    if artifact.byte_size != source.artifact_byte_size:
                        raise ValueError("source retained artifact size differs")
                if source.captured_source is None and "CAPTURED_SOURCE_UNAVAILABLE" not in record.gaps:
                    raise ValueError("missing original CapturedSource requires an explicit gap")
        preparations = {}
        for ref in plan.preparations:
            # Bound output bytes before existing verify owner reads/parses them.
            preparation = decode_object(self._read(ref.preparation_sha256, MAX_PREPARATION_BYTES, {}))
            recipe = decode_object(self._read(ref.recipe_sha256, MAX_ENVELOPE_BYTES, {}))
            for value, revision in ((recipe, "recipe_revision"), (preparation, "preparation_revision")):
                if type(value.get(revision)) is not int or value[revision] not in (1, 2) or type(value.get("engine_revision")) is not int:
                    raise ValueError("canonical contract revisions require exact integer identities")
                if value.get("engine_identity_sha256") != plan.canonical_engine_sha256:
                    raise ValueError("canonical preparation build differs from complete plan")
                for member in value["members"]:
                    for name in ("snapshot_id", "security_id"):
                        if type(member.get(name)) is not int or member[name] <= 0:
                            raise ValueError("canonical member requires exact integer identities")
                    for source in member.get("source_observations", []):
                        for name in ("source_item_id", "source_observation_id"):
                            if type(source.get(name)) is not int or source[name] <= 0:
                                raise ValueError("canonical source requires exact integer identities")
            verified = self.canonical.verify(ref.preparation_sha256, ref.recipe_sha256)
            preparations[(ref.recipe_sha256, ref.preparation_sha256)] = dict(members=[
                {key: member[key] for key in ("security_id", "snapshot_id", "snapshot_sha256", "source_observations")}
                for member in verified["members"]])
            del verified, preparation, recipe
        if accounting.expected_parent_release_id is not None:
            actual_parent = self.publications.retained_members(accounting.expected_parent_release_id)
            if set(actual_parent) != {m.identity() for m in accounting.parent_members} or len(actual_parent) != len(accounting.parent_members):
                raise ValueError("accounting differs from complete retained parent membership")
        selected_members = {m.security_id: m for m in accounting.selected_members}
        consumed = set()
        outcomes = {o.input_key: o for o in accounting.outcomes}
        for record in records:
            if accounting.expected_parent_release_id is None:
                if record.parent_state is not None:
                    raise ValueError("initial plan cannot invent prior accepted state")
            else:
                if record.parent_state is None or record.parent_state.parent_release_id != accounting.expected_parent_release_id:
                    raise ValueError("parent state gap must bind the named retained parent")
                selection = outcomes[record.input_key].selection
                prior = next((m for m in accounting.parent_members if selection is not None and m.security_id == selection.security_id), None)
                if (record.parent_state.security_id, record.parent_state.snapshot_id) != ((prior.security_id, prior.snapshot_id) if prior else (None, None)):
                    raise ValueError("parent state gap binds another retained member")
                if record.parent_state.artifact_sha256 is not None:
                    self._read(record.parent_state.artifact_sha256, MAX_PLAN_BYTES, {})
            allowed = inputs[record.input_key]
            outcome = outcomes[record.input_key]
            if (outcome.selection is None) != (record.canonical is None):
                raise ValueError("canonical association differs from actual accounting outcome")
            if record.canonical is None:
                continue
            ref = record.canonical
            pair = (ref.recipe_sha256, ref.preparation_sha256)
            output = preparations.get(pair)
            if output is None or ref.member_ordinal >= len(output["members"]):
                raise ValueError("canonical member association is absent")
            slot = pair + (ref.member_ordinal,)
            if slot in consumed:
                raise ValueError("canonical member consumed twice")
            consumed.add(slot)
            member = output["members"][ref.member_ordinal]
            binding = outcome.selection
            if type(member.get("security_id")) is not int or type(member.get("snapshot_id")) is not int or (member["security_id"], member["snapshot_id"]) != (binding.security_id, binding.snapshot_id):
                raise ValueError("canonical member differs from accounting selection")
            company = self.companies.snapshot(binding.snapshot_id)
            if company is None or (company.issuer_source, company.issuer_identifier) != (record.issuer.source, record.issuer.identifier):
                raise ValueError("canonical snapshot issuer differs from input")
            if not {a.content_sha256 for a in company.artifacts}.issubset(allowed):
                raise ValueError("input accounting omits canonical artifact dependencies")
            if member["snapshot_sha256"] != company.snapshot.snapshot_sha256:
                raise ValueError("canonical snapshot hash differs")
            # Every captured graph observation needs its actual delivery in this input.
            actual_sources = {s.delivery.source_observation_id for s in record.sources}
            selected = selected_members[binding.security_id]
            if selected.candidate_id is not None:
                candidate = self.publications.candidate(selected.candidate_id)
                if (candidate is None or candidate.security_id != binding.security_id
                        or candidate.snapshot_id != binding.snapshot_id
                        or candidate.expected_parent_release_id != accounting.expected_parent_release_id
                        or candidate.exact_bundle_id != binding.exact_bundle_id
                        or not set(candidate.source_observation_ids).issubset(actual_sources)):
                    raise ValueError("registered candidate differs from input/accounting association")
            if not {s["source_observation_id"] for s in member["source_observations"]}.issubset(actual_sources):
                raise ValueError("canonical source observation lacks a pinned delivery")
            if (binding.exact_bundle_id is None) != (record.exact_bundle is None):
                raise ValueError("accounting exact bundle differs from input")
            if record.exact_bundle is not None:
                exact = record.exact_bundle
                if exact.exact_bundle_id != binding.exact_bundle_id:
                    raise ValueError("exact bundle ID differs from accounting")
                with self.history.engine.connect() as connection:
                    identity = self.history.publication_identity_on_connection(connection, exact.exact_bundle_id,
                        security_id=binding.security_id, snapshot_id=binding.snapshot_id,
                        expected_run_key=exact.expected_exact_run_key)
                    bundle = self.history.load_on_connection(connection, exact.exact_bundle_id)
                if identity["exact_bundle_sha256"] != exact.exact_bundle_sha256 or not set(identity["source_observation_ids"]).issubset(actual_sources):
                    raise ValueError("exact bundle hash/source associations differ")
                occurrences = {(s.delivery.source_item_id, s.delivery.source_observation_id, s.delivery.occurrence_id) for s in record.sources}
                if any(s.occurrence_id is None or (s.source_item_id, s.source_observation_id, s.occurrence_id) not in occurrences for s in bundle.sources):
                    raise ValueError("exact bundle lacks its pinned source delivery occurrences")
        expected = {pair + (ordinal,) for pair, output in preparations.items() for ordinal in range(len(output["members"]))}
        if consumed != expected:
            raise ValueError("extra or unconsumed preparation members")
        if resolver_identity() != build:
            raise JobDeferred("complete resolver changed while resolving")
        return records

    def _plan(self, digest):
        payloads = {}
        raw = self._read(digest, MAX_ENVELOPE_BYTES, payloads)
        plan = load(CompleteInputPlan, raw)
        records = self._inputs(plan, raw, {})
        return plan, records

    def resolve(self, complete_plan_sha256):
        plan, records = self._plan(complete_plan_sha256)
        result = []
        for record in records:
            metadata = None
            if record.submissions is not None:
                if record.issuer.source != "SEC":
                    raise ValueError("SEC metadata cannot bind another issuer system")
                value = resolve_submissions(record.submissions, record.issuer.identifier,
                    artifacts=self.artifacts, objects=self.objects, observations=self.observations, payloads={})
                metadata = self._store(dict(contract="CAPTURED_SEC_METADATA1",
                    resolver_build_sha256=plan.resolver_build_sha256,
                    semantic_verification="MINTED_CHECKPOINT_TRUST_ONLY", result=value))
            result.append(dict(input=record.model_dump(mode="json"), metadata_sha256=metadata))
        if resolver_identity() != plan.resolver_build_sha256:
            raise JobDeferred("complete resolver changed before resolution output")
        output = load(Resolution, encode(dict(contract="COMPLETE_INPUT_RESOLUTION1",
            complete_plan_sha256=complete_plan_sha256, accounting_sha256=plan.accounting_sha256,
            resolver_build_sha256=plan.resolver_build_sha256, canonical_engine_sha256=plan.canonical_engine_sha256,
            scope="RESOLVER_ONLY", financial_status="NOT_ASSESSED", display_status="NOT_BUILT",
            engine_identity_status="DECLARED_NOT_VERIFIED", inputs=result, gaps=plan.gaps)))
        raw = encode(output.model_dump(mode="json"))
        if len(raw) > MAX_PLAN_BYTES:
            raise ValueError("complete resolution exceeds64MiB")
        return store_evidence(raw, RESOLUTION_MEDIA_TYPE, self.objects, self.artifacts).content_sha256

    def verify(self, resolution_sha256, complete_plan_sha256):
        """Verify retained associations. Metadata semantics trust the minted checkpoint."""
        plan, records = self._plan(complete_plan_sha256)
        output = load(Resolution, self._read(resolution_sha256, MAX_PLAN_BYTES, {}))
        if (output.complete_plan_sha256 != complete_plan_sha256 or output.accounting_sha256 != plan.accounting_sha256
                or output.resolver_build_sha256 != plan.resolver_build_sha256
                or output.canonical_engine_sha256 != plan.canonical_engine_sha256
                or output.gaps != plan.gaps or tuple(r.input for r in output.inputs) != tuple(records)):
            raise ValueError("resolution differs from exact plan associations")
        for resolved in output.inputs:
            if (resolved.metadata_sha256 is None) != (resolved.input.submissions is None):
                raise ValueError("resolution metadata presence differs from pinned input")
            if resolved.metadata_sha256 is not None:
                metadata = load(captured_metadata.MetadataArtifact,
                    self._read(resolved.metadata_sha256, MAX_PREPARATION_BYTES, {}))
                if metadata.resolver_build_sha256 != plan.resolver_build_sha256 or metadata.result.source != resolved.input.submissions:
                    raise ValueError("retained metadata identifies different source/build/delivery")
        if resolver_identity() != plan.resolver_build_sha256:
            raise JobDeferred("complete resolver changed during recovery")
        return output


class CompleteInputResolutionHandler:
    def __init__(self, resolver):
        self.resolver = resolver

    def __call__(self, context):
        parameters = context.job.parameters
        if set(parameters) != {"complete_plan_sha256"}:
            raise ValueError("complete resolution accepts only complete_plan_sha256")
        # Validate exact SHA before any owner lookup.
        digest = TypeAdapter(Sha).validate_python(parameters["complete_plan_sha256"], strict=True)
        if context.stopping.is_set():
            raise JobDeferred("complete resolution stopped")
        with context._repository.engine.begin() as connection:
            context._repository._lock_live_lease(connection, context.lease, context._clock)
        checkpoint = context.job.checkpoint
        if checkpoint is None:
            checkpoint = dict(complete_plan_sha256=digest, resolution_sha256=self.resolver.resolve(digest),
                              resolver_build_sha256=resolver_identity())
        else:
            if (set(checkpoint) != {"complete_plan_sha256", "resolution_sha256", "resolver_build_sha256"}
                    or checkpoint["complete_plan_sha256"] != digest
                    or checkpoint["resolver_build_sha256"] != resolver_identity()):
                raise ValueError("complete resolution checkpoint identifies different inputs")
            TypeAdapter(Sha).validate_python(checkpoint["resolution_sha256"], strict=True)
            self.resolver.verify(checkpoint["resolution_sha256"], digest)
        if context.stopping.is_set():
            raise JobDeferred("complete resolution stopped before checkpoint")
        context.save_checkpoint(checkpoint)
        context.record_item_outcome(item_key="complete-input-resolution", status="succeeded", outcome=checkpoint)
        return checkpoint
