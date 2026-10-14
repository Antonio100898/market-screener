"""Offline canonical preparation from immutable inputs; no display publication."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from . import artifacts, ch13, company_import, evidence, exact_annual, market_captures, models, normalize, primary_input_plan, primary_input_transport, sec_ingestion, store, sync
from . import figure_history, source_observations, source_validity, durable_jobs, postgres, shared_companies
from .artifacts import read_registered_artifact, store_evidence
from .company_import import _canonical_json, _verify_sec_ticker_mapping
from .durable_jobs import JobDeferred
from .evidence import EvidenceBundle
from .market_captures import MarketReplay
from .sec_ingestion import (_recent_filing_metadata, _security_identity,
    _SUBMISSION_PART_FINGERPRINT_BUDGET, _submission_part_source_hashes)
from .sources import cover, edinet, edinet_mapper, inline_xbrl, jpx, prices
from .sources import sec_json, sec_submission
from .sources.edinet import validate_xbrl_archive


Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PositiveID = Annotated[int, Field(gt=0)]
RECIPE_MEDIA_TYPE = "application/vnd.market-screener.canonical-preparation-input+json"
OUTPUT_MEDIA_TYPE = "application/vnd.market-screener.canonical-preparation+json"


class PreparationUnavailable(JobDeferred):
    """Required captured graph or compatible canonical result is unavailable."""


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    security_id: PositiveID
    snapshot_id: PositiveID
    market_recipe_sha256: Sha | None
    jpx_listing_sha256: Sha | None = None
    source_observation_sha256: list[Sha] = Field(default_factory=list, max_length=10000)


class InputRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recipe_revision: Annotated[int, Field(ge=1, le=1)]
    engine_revision: PositiveID
    engine_identity_sha256: Sha
    members: list[Member] = Field(min_length=1, max_length=10000)


class PlannedMember(Member):
    primary_input_plan_sha256: Sha
    company_facts_occurrence_id: PositiveID


class PlannedInputRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recipe_revision: Annotated[int, Field(ge=2, le=2)]
    engine_revision: PositiveID
    engine_identity_sha256: Sha
    members: list[PlannedMember] = Field(min_length=1, max_length=10000)


def _recipe(value):
    return PlannedInputRecipe.model_validate(value) if value.get("recipe_revision") == 2 else InputRecipe.model_validate(value)


def engine_identity() -> str:
    """Bind replay to the financial/parser source actually used by this process."""
    modules = (artifacts, ch13, company_import, evidence, exact_annual, market_captures, models, normalize, primary_input_plan, primary_input_transport, sec_ingestion,
               store, sync, cover, edinet, edinet_mapper, inline_xbrl, jpx, prices, sec_json, sec_submission,
               figure_history, source_observations, source_validity, durable_jobs, postgres, shared_companies)
    files = [Path(module.__file__) for module in modules]
    files.append(Path(__file__))
    files.extend(sorted((Path(__file__).parent / "screens").glob("*.py")))
    budget = _SUBMISSION_PART_FINGERPRINT_BUDGET.get()
    if budget is not None:
        entries = tuple((str(path.relative_to(Path(__file__).parent)), path) for path in files)
        hashes = _submission_part_source_hashes(entries, budget, owner_kind='engine', encode_result=True)
        return hashlib.sha256(_canonical_json(hashes)).hexdigest()
    return hashlib.sha256(_canonical_json({
        str(path.relative_to(Path(__file__).parent)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    })).hexdigest()


def _json(raw):
    return sec_json.decode_object(raw)


class CapturedCanonicalPreparer:
    def __init__(self, companies, artifacts, objects, observations):
        self.companies, self.artifacts = companies, artifacts
        self.objects, self.observations = objects, observations

    def _read(self, digest, *, maximum=None, verified_payloads=None):
        return read_registered_artifact(digest, self.objects, self.artifacts, maximum=maximum,
                                        label="preparation", verified_payloads=verified_payloads)[1]

    def prepare(self, recipe_sha256):
        recipe = _recipe(_json(self._read(recipe_sha256, maximum=1024 * 1024)))
        identity = engine_identity()
        if recipe.engine_revision != store.ENGINE_VERSION or recipe.engine_identity_sha256 != identity:
            raise PreparationUnavailable("captured engine identity differs from running engine")
        members, security_ids, tickers, issuers = [], set(), set(), set()
        for requested in recipe.members:
            company = self.companies.snapshot(requested.snapshot_id)
            if company is None:
                raise PreparationUnavailable("captured snapshot is unavailable")
            snap = company.snapshot
            issuer = (company.issuer_source, company.issuer_identifier)
            if snap.security_id != requested.security_id:
                raise ValueError("captured snapshot belongs to another security")
            if (requested.security_id in security_ids or snap.ticker in tickers or issuer in issuers):
                raise ValueError("duplicate preparation company identity")
            security_ids.add(requested.security_id); tickers.add(snap.ticker); issuers.add(issuer)
            if snap.engine_revision != recipe.engine_revision:
                raise PreparationUnavailable("snapshot requires a different engine")
            if isinstance(requested, PlannedMember):
                payloads = {}
                for link in company.artifacts:
                    self._read(link.content_sha256, verified_payloads=payloads)
            else:
                payloads = {link.content_sha256: self._read(link.content_sha256) for link in company.artifacts}
            roles = {}
            for link in company.artifacts:
                roles.setdefault(link.role, []).append(link.content_sha256)
            gaps = ["SOURCE_CURRENTNESS_NOT_ASSESSED", "OFFICIAL_FINANCIAL_READINESS_NOT_ASSESSED"]
            sources = self._sources(requested, company, payloads)
            planned = self._planned_inputs(requested, company, roles, payloads)
            if not sources and (planned is None or not planned.captured_sources):
                gaps.append("UPSTREAM_SOURCE_OBSERVATIONS_ABSENT")
            bundle = self._bundle(company, roles, payloads, gaps, requested, planned=planned)
            result, status, strict, strict_bytes = self._derive_and_match(bundle, snap, planned=planned)
            if result is None:
                assumed_status, assumed = sync._derive_evidence(bundle, assume_absent_zero=True)
            elif result.rq_result is not None:
                assumed_status, assumed = result.rq_result.status, result.rq_result.row
            else:
                assumed_status, assumed = "unavailable", None
            assumed_sha = None
            if assumed_status in {"ok", "pending_facts"} and assumed is not None:
                assumed_sha = self._store(_canonical_json(assumed), "application/json")
            else:
                gaps.append("ASSUMED_ENGINE_RESULT_UNAVAILABLE")
            market = self._market(requested, snap, strict, gaps)
            members.append(dict(security_id=snap.security_id, snapshot_id=snap.snapshot_id,
                snapshot_sha256=snap.snapshot_sha256, canonical_payload_sha256=snap.payload_sha256,
                canonical_artifact_sha256=self._store(strict_bytes, "application/json"),
                assumed_engine_artifact_sha256=assumed_sha, detail_projection_status="NOT_BUILT",
                market_artifact_sha256=self._store(_canonical_json(market), "application/json"),
                market_recipe_sha256=requested.market_recipe_sha256,
                jpx_listing_sha256=requested.jpx_listing_sha256,
                source_observations=sources, input_artifacts=[asdict(link) for link in company.artifacts],
                upstream_gaps=gaps))
            if planned is not None:
                members[-1]["primary_input_transport"] = planned.manifest
                members[-1]["company_facts_occurrence_id"] = requested.company_facts_occurrence_id
                gaps.append("PRIMARY_INPUT_FINANCIAL_CONSUMER_NOT_IMPLEMENTED")
        if engine_identity() != identity:
            raise PreparationUnavailable("financial/parser source changed during preparation")
        output = dict(preparation_revision=recipe.recipe_revision, recipe_sha256=recipe_sha256,
            engine_revision=recipe.engine_revision, engine_identity_sha256=identity,
            official_financial_readiness="NOT_ASSESSED", display_projection_status="NOT_BUILT", members=members)
        return self._store(_canonical_json(output), OUTPUT_MEDIA_TYPE)

    @staticmethod
    def _derive_and_match(bundle, snap, *, planned):
        result = None if planned is None else sync._derive_evidence_result(bundle)
        status, strict = sync._derive_evidence(bundle) if result is None else (result.status, result.row)
        if status != "ok" or strict is None:
            raise PreparationUnavailable(f"captured engine result unavailable: {status}")
        strict_bytes = _canonical_json(strict)
        if (hashlib.sha256(strict_bytes).hexdigest() != snap.payload_sha256
                or strict_bytes != _canonical_json(snap.canonical_payload)):
            raise PreparationUnavailable("captured inputs do not reproduce selected canonical snapshot")
        return result, status, strict, strict_bytes

    def _reconstruct_native(self, recipe_sha256, identity):
        raw = self._read(recipe_sha256, maximum=1024 * 1024)
        value, _ = durable_jobs._bounded_schedule_parameters(_json(raw))
        if (set(value) != {"recipe_revision", "engine_revision", "engine_identity_sha256", "members"}
                or type(value.get("recipe_revision")) is not int or value["recipe_revision"] != 2
                or type(value.get("members")) is not list or len(value["members"]) != 1):
            raise ValueError("native reconstruction requires one planned recipe member")
        primary_input_plan._integer(value["engine_revision"])
        primary_input_plan._digest(value["engine_identity_sha256"])
        member = value["members"][0]
        required_fields = {"security_id", "snapshot_id", "market_recipe_sha256", "primary_input_plan_sha256", "company_facts_occurrence_id"}
        if (type(member) is not dict or not required_fields.issubset(member)
                or not set(member).issubset(required_fields | {"jpx_listing_sha256", "source_observation_sha256"})):
            raise ValueError("native reconstruction member must be an object")
        primary_input_plan._digest(member["primary_input_plan_sha256"])
        for name in ("security_id", "snapshot_id", "company_facts_occurrence_id"):
            if type(member.get(name)) is not int or not 0 < member[name] < 2**63:
                raise ValueError("native reconstruction IDs must be exact signed64")
        references = member.get("source_observation_sha256", [])
        if type(references) is not list or len(references) > 64:
            raise ValueError("native reconstruction source reference ceiling exceeded")
        for digest in references:
            primary_input_plan._digest(digest)
        if member.get("market_recipe_sha256") is not None or member.get("jpx_listing_sha256") is not None:
            raise ValueError("native reconstruction does not admit market or JPX references")
        recipe = _recipe(value)
        requested = recipe.members[0]
        if recipe.engine_revision != store.ENGINE_VERSION or recipe.engine_identity_sha256 != identity:
            raise PreparationUnavailable("captured engine identity differs from running engine")
        company = self.companies.snapshot(requested.snapshot_id)
        if (company is None or company.issuer_source != "SEC"
                or company.snapshot.security_id != requested.security_id
                or company.snapshot.snapshot_id != requested.snapshot_id
                or company.snapshot.engine_revision != recipe.engine_revision):
            raise PreparationUnavailable("native reconstruction snapshot/security/engine unavailable")
        for name in ("snapshot_id", "security_id", "engine_revision"):
            if type(getattr(company.snapshot, name)) is not int or not 0 < getattr(company.snapshot, name) < 2**63:
                raise ValueError("native reconstruction snapshot IDs/revision must be exact signed64")
        durable_jobs._bounded_schedule_parameters(company.snapshot.canonical_payload)
        snapshot_state = self._snapshot_state(company.snapshot)
        roles = {}
        if len(company.artifacts) > 64:
            raise ValueError("native reconstruction artifact role ceiling exceeded")
        for link in company.artifacts:
            primary_input_plan._digest(link.content_sha256)
            durable_jobs._bounded_schedule_text("artifact role", link.role, 4096)
            roles.setdefault(link.role, []).append(link.content_sha256)
        if set(roles.get(primary_input_transport.PLAN_ROLE, ())) != {requested.primary_input_plan_sha256}:
            raise ValueError("native reconstruction requires the exact snapshot plan membership")
        cf_digest = self._one(roles, {link.content_sha256: link.content_sha256 for link in company.artifacts},
                              ("official_api_facts", "sec_root_company_facts"))
        plan_raw = self._read(requested.primary_input_plan_sha256, maximum=primary_input_plan.MAXIMUM_BYTES)
        plan = primary_input_plan.decode_native_preparation_plan(plan_raw)
        if plan.security_id != requested.security_id or plan.cik != company.issuer_identifier:
            raise ValueError("native reconstruction plan/security differs")
        required_links = {(requested.primary_input_plan_sha256, primary_input_transport.PLAN_ROLE),
                          *((dependency.artifact_sha256, primary_input_transport.DEPENDENCY_ROLE) for dependency in plan.dependencies)}
        if not required_links.issubset({(link.content_sha256, link.role) for link in company.artifacts}):
            raise ValueError("native reconstruction plan dependencies lack snapshot membership")
        transport = primary_input_transport.PrimaryInputTransport(self.artifacts, self.objects, self.observations)
        records = []
        for dependency in plan.dependencies:
            if dependency.captured_source is None or dependency.delivery is None or dependency.observation_sha256 is None:
                continue
            stored = self.observations.by_sha256(dependency.observation_sha256)
            transport._verify_dependency_metadata(dependency, stored, plan.cik)
            actual = self.observations.delivery(dependency.delivery.occurrence_id,
                source_item_id=dependency.delivery.source_item_id, source_observation_id=dependency.delivery.source_observation_id)
            transport._verify_dependency_delivery(dependency, actual)
            records.append((dependency, stored))
        transport._native_parent_context(records)
        choices = [d for d, _ in records if d.captured_source.role == "company_facts"
                   and d.captured_source.occurrence_id == requested.company_facts_occurrence_id]
        if len(choices) != 1 or choices[0].artifact_sha256 != cf_digest:
            raise ValueError("native reconstruction pinned CF differs from unique snapshot facts")
        uses = {recipe_sha256: {"control"}, requested.primary_input_plan_sha256: {"control"}}
        sizes = {}
        for link in company.artifacts:
            uses.setdefault(link.content_sha256, set()).add(link.role)
        for dependency in plan.dependencies:
            uses.setdefault(dependency.artifact_sha256, set()).update(dependency.roles)
            if dependency.artifact_sha256 in sizes and sizes[dependency.artifact_sha256] != dependency.byte_size:
                raise ValueError("native reconstruction dependency sizes conflict")
            sizes[dependency.artifact_sha256] = dependency.byte_size
        for mapping in plan.declarations:
            for request in (mapping.original, mapping.replacement):
                if request is not None:
                    uses.setdefault(request.source.artifact_sha256, set()).add("native")
            for document in mapping.documents:
                uses.setdefault(document.reference.artifact_sha256, set()).add("document")
        for dependency, stored in records:
            if stored.item.item_kind == "filing":
                uses.setdefault(dependency.artifact_sha256, set()).add("filing")
        if len(uses) > 32:
            raise ValueError("native reconstruction whole union exceeds32 artifacts")
        metadata, maxima = {}, {}
        cf_roles = {"official_api_facts", "sec_root_company_facts", "company_facts", "target", "primary_input_dependency"}
        for digest, meanings in uses.items():
            maximum = 32 * 1024 * 1024 if digest == cf_digest and meanings <= cf_roles else 8 * 1024 * 1024
            if "control" in meanings:
                maximum = min(maximum, 1024 * 1024)
            artifact = self.artifacts.get(digest)
            if (artifact.content_sha256 != digest or artifact.object_key != "raw/sha256/" + digest
                    or type(artifact.byte_size) is not int or artifact.byte_size < 0 or artifact.byte_size > maximum
                    or digest in sizes and sizes[digest] != artifact.byte_size):
                raise ValueError("native reconstruction union metadata/ceiling differs")
            durable_jobs._bounded_schedule_text("registered media type", artifact.media_type, 4096)
            metadata[digest] = transport._artifact_identity(artifact)
            maxima[digest] = maximum
        if sum(row["byte_size"] for row in metadata.values()) > 64 * 1024 * 1024:
            raise ValueError("native reconstruction complete union exceeds64MiB")
        inputs = transport.resolve_registered_native(requested.primary_input_plan_sha256,
            cik=company.issuer_identifier, company_facts_occurrence_id=requested.company_facts_occurrence_id,
            security_identifier=company.security_identifier)
        if not set(inputs.resolved.artifacts).issubset(company.artifacts):
            raise ValueError("native reconstruction transport artifacts differ from snapshot graph")
        payloads = {}
        for link in company.artifacts:
            self._read(link.content_sha256, maximum=maxima[link.content_sha256], verified_payloads=payloads)
        gaps = ["SOURCE_CURRENTNESS_NOT_ASSESSED", "OFFICIAL_FINANCIAL_READINESS_NOT_ASSESSED"]
        sources = self._sources(requested, company, payloads)
        bundle = self._bundle(company, roles, payloads, gaps, requested, planned=inputs.resolved)
        result, _, _, strict_bytes = self._derive_and_match(bundle, company.snapshot, planned=inputs.resolved)
        if result.snapshot is None:
            raise PreparationUnavailable("native reconstruction requires actual successful strict Facts")
        return dict(recipe=recipe, requested=requested, company=company, inputs=inputs, bundle=bundle,
                    result=result, strict_bytes=strict_bytes, metadata=metadata, gaps=gaps, sources=sources,
                    snapshot_state=snapshot_state)

    @staticmethod
    def _snapshot_state(snapshot):
        return _canonical_json({name: str(value) if isinstance(value, Decimal) else
            value.isoformat() if hasattr(value, "isoformat") else value for name, value in vars(snapshot).items()})

    def _recheck_reconstruction(self, prepared):
        requested, original = prepared["requested"], prepared["company"]
        current = self.companies.snapshot(requested.snapshot_id)
        if (current is None or current.issuer_source != original.issuer_source
                or current.issuer_identifier != original.issuer_identifier
                or current.security_identifier != original.security_identifier
                or current.artifacts != original.artifacts
                or self._snapshot_state(current.snapshot) != prepared["snapshot_state"]
                or _canonical_json(current.snapshot.canonical_payload) != prepared["strict_bytes"]
                or any(getattr(current.snapshot, name) != getattr(original.snapshot, name) for name in
                       ("security_id", "snapshot_id", "snapshot_sha256", "payload_sha256", "engine_revision"))):
            raise ValueError("native reconstruction snapshot/security graph changed")
        transport = primary_input_transport.PrimaryInputTransport(self.artifacts, self.objects, self.observations)
        records = []
        for dependency in prepared["inputs"].plan.dependencies:
            if dependency.captured_source is None or dependency.delivery is None or dependency.observation_sha256 is None:
                continue
            stored = self.observations.by_sha256(dependency.observation_sha256)
            transport._verify_dependency_metadata(dependency, stored, current.issuer_identifier)
            actual = self.observations.delivery(dependency.delivery.occurrence_id,
                source_item_id=dependency.delivery.source_item_id, source_observation_id=dependency.delivery.source_observation_id)
            transport._verify_dependency_delivery(dependency, actual)
            records.append((dependency, stored))
        transport._native_parent_context(records)
        if _canonical_json(self._sources(requested, current, {digest: None for digest in prepared["metadata"]})) != _canonical_json(prepared["sources"]):
            raise ValueError("native reconstruction recipe source observations changed")
        for digest, metadata in prepared["metadata"].items():
            if transport._artifact_identity(self.artifacts.get(digest)) != metadata:
                raise ValueError("native reconstruction artifact metadata changed")

    def verify(self, preparation_sha256, recipe_sha256):
        output = _json(self._read(preparation_sha256))
        recipe = _recipe(_json(self._read(recipe_sha256, maximum=1024 * 1024)))
        expected = {"preparation_revision", "recipe_sha256", "engine_revision", "engine_identity_sha256",
                    "official_financial_readiness", "display_projection_status", "members"}
        if isinstance(recipe, PlannedInputRecipe):
            identity = dict(preparation_revision=recipe.recipe_revision, recipe_sha256=recipe_sha256,
                engine_revision=recipe.engine_revision, engine_identity_sha256=recipe.engine_identity_sha256,
                official_financial_readiness="NOT_ASSESSED", display_projection_status="NOT_BUILT")
            if _canonical_json({key: output.get(key) for key in identity}) != _canonical_json(identity):
                raise ValueError("preparation checkpoint output identifies different inputs or contract")
        if (set(output) != expected or output["preparation_revision"] != recipe.recipe_revision
                or output["recipe_sha256"] != recipe_sha256 or output["engine_revision"] != recipe.engine_revision
                or output["engine_identity_sha256"] != recipe.engine_identity_sha256
                or output["official_financial_readiness"] != "NOT_ASSESSED"
                or output["display_projection_status"] != "NOT_BUILT"
                or not isinstance(output["members"], list) or len(output["members"]) != len(recipe.members)):
            raise ValueError("preparation checkpoint output identifies different inputs or contract")
        if recipe.engine_revision != store.ENGINE_VERSION or recipe.engine_identity_sha256 != engine_identity():
            raise PreparationUnavailable("checkpoint engine identity differs from running engine")
        for member, requested in zip(output["members"], recipe.members):
            if isinstance(requested, PlannedMember):
                identity = dict(snapshot_id=requested.snapshot_id, security_id=requested.security_id,
                    market_recipe_sha256=requested.market_recipe_sha256, jpx_listing_sha256=requested.jpx_listing_sha256,
                    detail_projection_status="NOT_BUILT")
                if _canonical_json({key: member.get(key) for key in identity}) != _canonical_json(identity):
                    raise ValueError("preparation member differs from checkpoint recipe")
            if (member["snapshot_id"] != requested.snapshot_id or member["security_id"] != requested.security_id
                    or member["market_recipe_sha256"] != requested.market_recipe_sha256
                    or member["jpx_listing_sha256"] != requested.jpx_listing_sha256
                    or member["detail_projection_status"] != "NOT_BUILT"):
                raise ValueError("preparation member differs from checkpoint recipe")
            company = self.companies.snapshot(requested.snapshot_id)
            if company is None or company.snapshot.security_id != requested.security_id:
                raise PreparationUnavailable("checkpoint snapshot is unavailable")
            snapshot = company.snapshot
            if isinstance(requested, PlannedMember):
                payloads = {}
                for link in company.artifacts:
                    self._read(link.content_sha256, verified_payloads=payloads)
            else:
                payloads = {link.content_sha256: self._read(link.content_sha256) for link in company.artifacts}
            if (snapshot.engine_revision != recipe.engine_revision
                    or member["snapshot_sha256"] != snapshot.snapshot_sha256
                    or member["canonical_payload_sha256"] != snapshot.payload_sha256
                    or (_canonical_json(member["input_artifacts"]) != _canonical_json([asdict(link) for link in company.artifacts])
                        if isinstance(requested, PlannedMember) else member["input_artifacts"] != [asdict(link) for link in company.artifacts])
                    or (_canonical_json(member["source_observations"]) != _canonical_json(self._sources(requested, company, payloads))
                        if isinstance(requested, PlannedMember) else member["source_observations"] != self._sources(requested, company, payloads))):
                raise ValueError("checkpoint member differs from immutable snapshot graph")
            if isinstance(requested, PlannedMember):
                roles = {}
                for link in company.artifacts:
                    roles.setdefault(link.role, []).append(link.content_sha256)
                planned = self._planned_inputs(requested, company, roles, payloads)
                if (_canonical_json(member.get("primary_input_transport")) != _canonical_json(planned.manifest)
                        or _canonical_json(member.get("company_facts_occurrence_id")) != _canonical_json(requested.company_facts_occurrence_id)):
                    raise ValueError("checkpoint private plan or admission differs")
            elif "primary_input_transport" in member or "company_facts_occurrence_id" in member:
                raise ValueError("private plan semantics require preparation revision2")
            if (hashlib.sha256(self._read(member["canonical_artifact_sha256"])).hexdigest()
                    != company.snapshot.payload_sha256):
                raise ValueError("checkpoint canonical artifact differs from snapshot")
            self._read(member["market_artifact_sha256"])
            if member["assumed_engine_artifact_sha256"] is not None:
                self._read(member["assumed_engine_artifact_sha256"])
        return output

    def _store(self, data, media_type):
        return store_evidence(data, media_type, self.objects, self.artifacts).content_sha256

    def _sources(self, requested, company, payloads):
        if len(set(requested.source_observation_sha256)) != len(requested.source_observation_sha256):
            raise ValueError("duplicate captured source observation")
        sources = []
        for digest in requested.source_observation_sha256:
            source = self.observations.by_sha256(digest)
            item, observation = source.item, source.observation
            if (item.source_system != company.issuer_source
                    or item.issuer_source_identifier != company.issuer_identifier
                    or observation.state != "present" or observation.artifact_sha256 not in payloads):
                raise ValueError("source observation does not bind this captured company graph")
            sources.append(dict(observation_sha256=digest, source_observation_id=observation.source_observation_id,
                source_item_id=item.source_item_id, artifact_sha256=observation.artifact_sha256))
        return sources

    def _one(self, roles, payloads, names, *, required=True):
        hashes = {digest for name in names for digest in roles.get(name, ())}
        if not hashes and not required:
            return None
        if len(hashes) != 1:
            raise PreparationUnavailable("required captured role is missing or ambiguous: " + ",".join(names))
        return payloads[next(iter(hashes))]

    def _planned_inputs(self, requested, company, roles, payloads):
        if not isinstance(requested, PlannedMember):
            return None
        if company.issuer_source != "SEC":
            raise PreparationUnavailable("private primary input plan requires SEC issuer")
        resolved = primary_input_transport.PrimaryInputTransport(self.artifacts, self.objects, self.observations).resolve(
            requested.primary_input_plan_sha256, cik=company.issuer_identifier,
            security_id=requested.security_id, company_facts_source=None,
            company_facts_payload=self._one(roles, payloads, ("official_api_facts", "sec_root_company_facts")),
            company_facts_occurrence_id=requested.company_facts_occurrence_id, payloads=payloads)
        if not set(resolved.artifacts).issubset(company.artifacts):
            raise ValueError("private plan dependencies differ from immutable snapshot graph")
        return resolved

    def _bundle(self, company, roles, payloads, gaps, requested, *, planned=None):
        snap, cik = company.snapshot, company.issuer_identifier
        if company.issuer_source == "EDINET":
            facts = _json(self._one(roles, payloads, ("canonical_edinet_facts",)))
            adapter = facts.get("_adapter") or {}
            if (facts.get("cik") != cik or adapter.get("kind") != edinet_mapper.ADAPTER_KIND
                    or adapter.get("ticker") != snap.ticker or adapter.get("security_basis") != snap.security_basis
                    or adapter.get("quote_currency") != snap.quote_currency
                    or adapter.get("security_code") != snap.ticker.removesuffix(".T") + "0"):
                raise ValueError("retained EDINET identity differs from selected security")
            reports = adapter.get("reports") or []
            if not reports or snap.source_accession not in {report.get("document") for report in reports}:
                raise PreparationUnavailable("EDINET canonical report graph is unavailable")
            archives = roles.get("raw_filing", ())
            if not archives:
                raise PreparationUnavailable("retained EDINET raw archive is unavailable")
            for digest in archives:
                validate_xbrl_archive(payloads[digest], snap.source_accession)
            gaps.append("EDINET_CANONICAL_ONLY_ORIGINAL_LIST_AND_REMAP_UNVERIFIED")
            if requested.jpx_listing_sha256 is None:
                gaps.append("JPX_RAW_LISTING_CAPTURE_ABSENT")
            else:
                listing = jpx.listed_companies(self._read(requested.jpx_listing_sha256)).get(snap.ticker.removesuffix(".T"))
                if listing is None:
                    raise PreparationUnavailable("captured JPX workbook does not list selected primary equity")
                gaps.append("JPX_CAPTURE_ASOF_IS_NOT_A_CURRENTNESS_GUARANTEE")
            return EvidenceBundle(cik, snap.ticker, facts, None, None, "not_sec")
        if company.issuer_source != "SEC":
            raise PreparationUnavailable("unsupported captured issuer source")
        raw_facts = self._one(roles, payloads, ("official_api_facts", "sec_root_company_facts"))
        facts_hash = hashlib.sha256(raw_facts).hexdigest()
        captured = []
        for digest in requested.source_observation_sha256 if planned is None else ():
            stored = self.observations.by_sha256(digest)
            if (stored.observation.artifact_sha256 == facts_hash
                    and getattr(stored.observation, "source_url", None)):
                captured.append(models.CapturedSource("company_facts", facts_hash,
                    stored.observation.source_url, stored.item.source_item_id,
                    stored.observation.source_observation_id, fidelity="DECODED_DECIMAL"))
        if len(captured) > 1:
            raise PreparationUnavailable("captured Company Facts source is ambiguous")
        facts = (planned.original_company_facts if planned is not None else
                 sec_json.decode_company_facts(raw_facts, source=captured[0]) if captured else _json(raw_facts))
        if str(facts.get("cik")).lstrip("0") != cik.lstrip("0"):
            raise ValueError("captured Company Facts identify another issuer")
        dimensioned_raw = self._one(roles, payloads, ("dimensioned_facts",), required=False)
        dimensioned = _json(dimensioned_raw) if dimensioned_raw is not None else None
        gaps.append("DERA_RAW_QUARTER_BINDING_UNVERIFIED" if dimensioned is not None else "DERA_COVERAGE_UNASSESSED")
        receipt_raw = self._one(roles, payloads, ("structured_cover_identity",), required=False)
        if receipt_raw is not None:
            receipt = _json(receipt_raw)
            gaps.append("RETAINED_STRUCTURED_COVER_RAW_RECONSTRUCTION_UNVERIFIED")
        elif snap.security_basis == "SEC_TICKER_MAPPING_ONLY":
            mapping = _json(self._one(roles, payloads, ("official_sec_ticker_mapping",)))
            _verify_sec_ticker_mapping(mapping, cik, snap.ticker)
            receipt = None
            gaps.append("RETAINED_SEC_MAPPING_COVER_UNAVAILABLE")
        else:
            submissions = _json(self._one(roles, payloads, ("sec_root_submissions",)))
            documents = [payloads[digest] for role, digests in roles.items()
                if role in {"sec_primary_document", "sec_cover_r1", "sec_cover_r2", "sec_cover_r3"} for digest in digests]
            filing = _recent_filing_metadata(submissions, snap.source_accession)
            if filing is None:
                raise PreparationUnavailable("captured SEC filing metadata is unavailable")
            ticker, exchange, receipt = _security_identity(submissions, accession=snap.source_accession,
                form=filing["form"], cover_payloads=documents)
            if ticker != snap.ticker or exchange != snap.exchange_code:
                raise ValueError("captured SEC raw cover differs from selected security")
        if receipt is not None:
            title = receipt.get("title") or ""
            if (not cover.is_common_equity_security(title) or cover.is_untraded_underlying(title)
                    or not cover.symbol_matches(receipt.get("symbol") or "", title, snap.ticker)
                    or receipt.get("accn") != snap.source_accession or title != snap.security_title):
                raise ValueError("captured cover identity differs from selected security")
            ratio = receipt.get("ratio")
            if (None if ratio is None else Decimal(str(ratio))) != snap.receipt_ratio:
                raise ValueError("captured receipt ratio differs from selected security")
            if cover.is_depositary_security(title) and snap.receipt_ratio is None:
                raise PreparationUnavailable("captured receipt ratio is unavailable")
        foreign_inline_roles = ("sec_inline_current_manifest", "sec_inline_direct_annual_",
                                "sec_inline_annual_wrapper_", "sec_inline_incorporated_source_")
        if any(any(role == prefix or role.startswith(prefix) for prefix in foreign_inline_roles) for role in roles):
            raise PreparationUnavailable("active foreign-inline graph requires full verified-byte owner validation")
        return EvidenceBundle(cik, snap.ticker, facts, dimensioned, receipt, "missing",
            tuple(captured) if planned is None else planned.captured_sources,
            facts if captured or planned is not None else None,
            () if planned is None else planned.outcomes)

    def _market(self, requested, snap, strict, gaps):
        if requested.market_recipe_sha256 is None:
            gaps.append("MARKET_CAPTURE_ABSENT")
            return dict(status="UNAVAILABLE", history=None, fx=None, requests=[])
        if snap.quote_currency is None:
            raise PreparationUnavailable("selected security quote currency is unavailable")
        replay = MarketReplay(requested.market_recipe_sha256, self.objects, self.artifacts)
        provider = prices.YahooPriceProvider(http=replay, clock=replay.now)
        history = provider.history(snap.ticker, expected_currency=snap.quote_currency)
        reporting = strict.get("reporting_currency") or strict.get("currency")
        fx = None
        if reporting is None:
            gaps.append("EXPLICIT_REPORTING_CURRENCY_UNAVAILABLE")
        elif reporting != snap.quote_currency:
            fx = provider.exchange_rate_history(snap.quote_currency, reporting)
        if history is None:
            gaps.append("CAPTURED_MARKET_HISTORY_UNAVAILABLE")
        if reporting and reporting != snap.quote_currency and fx is None:
            gaps.append("CAPTURED_FX_UNAVAILABLE")
        return dict(status="AVAILABLE" if history is not None else "UNAVAILABLE",
            history=None if history is None else json.loads(json.dumps(asdict(history), default=str)),
            fx=None if fx is None else json.loads(json.dumps(asdict(fx), default=str)), requests=replay.requests)


class CapturedPreparationHandler:
    """Optional durable preparation handler; production registration is separate."""
    def __init__(self, preparer):
        self.preparer = preparer

    def __call__(self, context):
        if set(context.job.parameters) != {"recipe_sha256"}:
            raise ValueError("preparation job accepts only recipe_sha256")
        if context.stopping.is_set():
            raise PreparationUnavailable("canonical preparation stopped")
        with context._repository.engine.begin() as connection:
            context._repository._lock_live_lease(connection, context.lease, context._clock)
        recipe = context.job.parameters["recipe_sha256"]
        checkpoint = context.job.checkpoint
        if checkpoint is not None:
            if set(checkpoint) != {"recipe_sha256", "preparation_sha256"} or checkpoint["recipe_sha256"] != recipe:
                raise ValueError("preparation checkpoint identifies different inputs")
            self.preparer.verify(checkpoint["preparation_sha256"], recipe)
        else:
            checkpoint = dict(recipe_sha256=recipe, preparation_sha256=self.preparer.prepare(recipe))
        if context.stopping.is_set():
            raise PreparationUnavailable("canonical preparation stopped before checkpoint")
        context.save_checkpoint(checkpoint)
        context.record_item_outcome(item_key="canonical-preparation", status="succeeded", outcome=checkpoint)
        return checkpoint
