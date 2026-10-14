"""Translate selected annual Facts; never select or calculate financial inputs."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from decimal import Decimal
import hashlib
import re

from .figure_history import (BundleSource, Coverage, ExactFigureBundle, FigureSelection,
                             Period, ReportedObservation, Scope, SecJsonLocation,
                             ShareBasis, SourceOccurrenceRef, Unit)
from .models import RawFactOrigin
from .sources.sec_json import verify_origin


FAMILIES = ("annual_eps", "annual_net_income", "annual_revenue",
            "annual_gross_profit", "annual_operating_income", "annual_share_counts")
SCOPE = Scope(consolidation="UNKNOWN", dimensions_assessed=False)
UNKNOWN_BASIS = ShareBasis(kind="UNKNOWN", incorporation="UNKNOWN")


def _unit(text):
    if text == "shares":
        return Unit(status="KNOWN", numerator=("shares",))
    if re.fullmatch(r"[A-Z]{3}(?:/shares)?", text or ""):
        currency = text[:3]
        return Unit(status="KNOWN", numerator=(f"iso4217:{currency}",),
                    denominator=("shares",) if text.endswith("/shares") else (),
                    currency=currency)
    return Unit(status="UNKNOWN")


def _period(start, end):
    try:
        if start and end:
            return Period(kind="DURATION", start=date.fromisoformat(start),
                          end=date.fromisoformat(end), origin="REPORTED")
        if end:
            return Period(kind="INSTANT", end=date.fromisoformat(end), origin="REPORTED")
    except ValueError:
        pass
    return Period(kind="UNKNOWN", origin="UNKNOWN")


@dataclass(frozen=True)
class PreparedAnnual:
    observations: tuple
    selections: tuple[dict, ...]
    coverage: tuple
    sources: tuple

    def bind(self, stored, *, pipeline_run_key, security_basis, receipt_ratio):
        return ExactFigureBundle(
            security_id=stored.security_id, snapshot_id=stored.snapshot_id,
            snapshot_sha256=stored.snapshot_sha256, engine_revision=stored.engine_revision,
            pipeline_run_key=pipeline_run_key, contract_revision=2,
            observations=self.observations,
            selections=tuple(FigureSelection(**node, security_basis=security_basis,
                receipt_ratio_text=str(receipt_ratio) if receipt_ratio is not None else None)
                for node in self.selections), coverage=self.coverage, sources=self.sources)


def prepare(result, captured_sources, *, cik, original_facts=None):
    if result.snapshot is None or result.status not in {"ok", "pending_facts"}:
        raise ValueError("annual history requires the actual successful Snapshot")
    known = {source for source in captured_sources}
    observations, nodes, coverage = {}, [], []
    views = [("STRICT", result)]
    if result.rq_result is not None and result.rq_result.snapshot is not None:
        views.append(("ASSUMED", result.rq_result))
    for view, derived in views:
        for family in FAMILIES:
            paths, fidelities = [], set()
            per_share = family in {"annual_eps", "annual_share_counts"}
            for year, fact in sorted(getattr(derived.snapshot, family).items()):
                path = f"{family}.{year}"
                paths.append(path)
                origin = fact.exact_origin
                reason = fact.exact_unassessed_reason
                unit = _unit(fact.provenance.unit)
                period = _period(
                    fact.provenance.period_start.isoformat() if fact.provenance.period_start else None,
                    fact.provenance.period_end.isoformat() if fact.provenance.period_end else None)
                observed_key = None
                if isinstance(origin, RawFactOrigin):
                    observed = _raw_observation(origin, known, cik=cik, original_facts=original_facts, per_share=per_share)
                    observed_key = observed.key
                    if observed_key in observations and observations[observed_key] != observed:
                        raise ValueError("same raw occurrence has conflicting annotation")
                    observations[observed_key] = observed
                    fidelities.add(origin.source.fidelity)
                    raw_value = Decimal(origin.lexical_text)
                    if (raw_value != fact.value or raw_value.is_zero() and fact.value.is_zero()
                            and raw_value.is_signed() != fact.value.is_signed()):
                        reason = reason or "COMPATIBILITY_PRECISION_LOSS"
                    if observed.period != period or observed.unit != unit:
                        reason = reason or "SELECTED_CONTEXT_DIFFERS"
                else:
                    reason = reason or "UNTRACED_OPERATION_OR_SOURCE"
                    fidelities.add("UNKNOWN")
                if per_share:
                    reason = reason or "unknown-incorporation"
                if period.kind == "UNKNOWN" or unit.status == "UNKNOWN":
                    reason = reason or "UNKNOWN_PERIOD_OR_UNIT"
                nodes.append(dict(node_key=f"{view}:{path}", metric_family=family,
                    metric_path=path, view=view, state="NOT_ASSESSED" if reason else "REPORTED",
                    value_decimal_text=None if reason else str(fact.value),
                    observation_key=None if reason else observed_key,
                    reason=reason, unit=unit, period=period, scope=SCOPE,
                    share_basis=UNKNOWN_BASIS if per_share else None))
            coverage.append(Coverage(metric_family=family, view=view, assessment="PARTIAL",
                metric_paths=tuple(paths) or (family,),
                reason="unknown-incorporation" if per_share else "COMPANY_FACTS_NATIVE_CONTEXT_UNAVAILABLE",
                source_fidelity=next(iter(fidelities)) if len(fidelities) == 1 else "UNKNOWN",
                earlier_history="CAPTURED_ONLY"))
    if len(views) == 1:
        coverage.extend(Coverage(metric_family=family, view="ASSUMED", assessment="NOT_ASSESSED",
            metric_paths=(family,), reason="ASSUMED_SNAPSHOT_UNAVAILABLE", source_fidelity="UNKNOWN",
            earlier_history="UNKNOWN") for family in FAMILIES)
    return PreparedAnnual(tuple(observations.values()), tuple(nodes), tuple(coverage),
        tuple(BundleSource(source_item_id=s.source_item_id, source_observation_id=s.source_observation_id,
                          occurrence_id=s.occurrence_id) for s in captured_sources))


def _raw_observation(origin, known, *, cik, original_facts, per_share):
    verify_origin(origin, original_facts)
    if origin.source not in known:
        raise ValueError("selected occurrence lacks captured source binding")
    key = hashlib.sha256((origin.source.artifact_sha256 + origin.pointer).encode()).hexdigest()
    return ReportedObservation(key=key,
        reference=SourceOccurrenceRef(artifact_sha256=origin.source.artifact_sha256,
            extraction_revision="sec-json-annual-v1", source_system="SEC",
            issuer_source_identifier=cik, source_document=origin.source.source_document,
            location=SecJsonLocation(pointer=origin.pointer), fidelity=origin.source.fidelity),
        qname=f"{origin.namespace}:{origin.concept}", value_decimal_text=origin.lexical_text,
        source_lexical_text=origin.lexical_text, unit=_unit(origin.unit),
        period=_period(origin.start, origin.end), scope=SCOPE,
        share_basis=UNKNOWN_BASIS if per_share else None)


def _prepare_reconstructed(result, captured_sources, *, cik, original_facts, envelope_reserve=0):
    from .models import Fact, UnavailablePrimaryInput
    from . import figure_history as history
    from .durable_jobs import _bounded_schedule_text
    known = set(captured_sources)
    if len(known) > 64:
        raise ValueError("reconstructed source count exceeds64")
    roots, graph, heights, edges = [], {}, {}, 0
    views = [("STRICT", result)]
    if result.rq_result is not None and result.rq_result.snapshot is not None and result.rq_result.status in {"ok", "pending_facts"}:
        views.append(("ASSUMED", result.rq_result))
    for view, derived in views:
        for family in FAMILIES:
            series = getattr(derived.snapshot, family)
            unavailable = getattr(series, "unavailable", {})
            if len(roots) + len(series) + len(unavailable) > 128:
                raise ValueError("reconstructed output count exceeds128")
            for year in sorted(set(series) | set(unavailable)):
                if type(year) is not int or not 0 < year <= 9999:
                    raise ValueError("invalid reconstructed annual year")
                roots.append((view, family, year, series.get(year), unavailable.get(year)))
    for _, _, _, root, _ in roots:
        if root is None:
            continue
        stack, active = [(root, 0, False)], set()
        while stack:
            fact, depth, exiting = stack.pop()
            key = id(fact)
            if exiting:
                heights[key] = 0 if not graph[key][1] else 1 + max(heights[id(child)] for child in graph[key][1])
                active.remove(key)
                continue
            if key in active:
                raise ValueError("reconstructed operand cycle")
            if depth > 32 or not isinstance(fact, Fact):
                raise ValueError("reconstructed Fact/depth ceiling differs")
            if key in graph:
                if depth + heights[key] > 32:
                    raise ValueError("reconstructed shared operand depth exceeds32")
                continue
            if len(graph) >= 4096:
                raise ValueError("reconstructed Fact count exceeds4096")
            children = fact.operation_inputs
            if type(children) is not tuple:
                raise ValueError("reconstructed operands must be an actual tuple")
            if len(children) > 8192 - edges:
                raise ValueError("reconstructed operand count exceeds8192")
            eps = fact.derived_eps_inputs
            if eps is not None:
                children = (*children, eps.income, eps.count, *((eps.preferred,) if eps.preferred is not None else ()))
            if edges + len(children) > 8192 or len(stack) + len(children) + 1 > 8192:
                raise ValueError("reconstructed operand frontier exceeds8192")
            edges += len(children)
            graph[key] = (fact, children)
            active.add(key)
            stack.append((fact, depth, True))
            stack.extend((child, depth + 1, False) for child in reversed(children))
    specifications, documents, operand_gaps = [], {}, []
    for view, family, year, root, unavailable in roots:
        reason = (root.exact_unassessed_reason if root is not None else None) or "RECONSTRUCTED_SELECTION_NOT_ASSESSED"
        if root is None:
            if not isinstance(unavailable, UnavailablePrimaryInput):
                raise ValueError("unavailable annual entry lacks its actual typed reason")
            reason = unavailable.reason
        _bounded_schedule_text("selection reason", reason, 4096)
        supported, seen, pending = [], set(), [] if root is None else [root]
        while pending:
            fact = pending.pop()
            if id(fact) in seen:
                continue
            seen.add(id(fact))
            pending.extend(graph[id(fact)][1])
            if fact.derived_eps_inputs is not None and fact.derived_eps_inputs.absent_preferred_reason is not None:
                _bounded_schedule_text("absent preferred reason", fact.derived_eps_inputs.absent_preferred_reason, 4096)
                operand_gaps.append((f"{view}:{family}.{year}", "preferred", fact.derived_eps_inputs.absent_preferred_reason))
            origin = fact.exact_origin
            if isinstance(origin, RawFactOrigin):
                supported.append(("cf", origin, "OPERAND_SOURCE" if fact is not root else "ORIGINAL_COMPANY_FACTS"))
            primary = fact.primary_input
            if primary is not None:
                if len(supported) + 3 + len(primary.mapping.documents) > 300:
                    raise ValueError("reconstructed witness frontier exceeds300")
                supported.append(("cf", primary.mapping.target_origin, "ORIGINAL_COMPANY_FACTS"))
                supported.append(("native", primary.original, "ORIGINAL_NATIVE" if fact is root else "OPERAND_SOURCE"))
                if primary.replacement is not None:
                    supported.append(("native", primary.replacement, "REPLACEMENT_NATIVE" if fact is root else "OPERAND_SOURCE"))
                for document in primary.mapping.documents:
                    if document.key in documents and documents[document.key] != document:
                        raise ValueError("reconstructed document conflict")
                    documents[document.key] = document
                    supported.append(("document", document, "CORRECTION_EXPLANATION"
                        if "CORRECTION_EXPLANATION" in document.roles else "DOCUMENT_SUPPORT"))
            if len(supported) > 300 or len(documents) > 64:
                raise ValueError("reconstructed witness/document ceiling exceeded")
        if unavailable is not None and unavailable.target_origin is not None:
            supported.append(("cf", unavailable.target_origin, "ORIGINAL_COMPANY_FACTS"))
        if len(supported) > 300:
            raise ValueError("reconstructed unavailable witness frontier exceeds300")
        for kind, value, _ in supported:
            if kind == "cf":
                if not isinstance(value, RawFactOrigin):
                    raise ValueError("reconstructed CF support lacks its original origin")
                for text in (value.pointer, value.namespace, value.concept, value.source.source_document):
                    _bounded_schedule_text("CF source name", text, 4096)
                _bounded_schedule_text("CF lexical text", value.lexical_text, 16384)
                verify_origin(value, original_facts)
                if value.source not in known:
                    raise ValueError("reconstructed CF source differs from admitted union")
            elif kind == "native":
                _bounded_schedule_text("native lexical text", value.lexical_text, 16384)
                _bounded_schedule_text("native decoded text", value.decoded_text, 16384)
                if value.request.source.captured_source not in known:
                    raise ValueError("reconstructed native source differs from admitted union")
        specifications.append((view, family, year, root, reason, supported))
    # Conservative pre-factory reservation includes escaped source text and witness keys.
    reservation, charged = envelope_reserve + 32768, set()
    for _, _, _, _, reason, supported in specifications:
        node_charge = 2048 + len(history._contract3_json(reason))
        for kind, value, role in supported:
            if kind == "document":
                source_options = [s for s in known if s.artifact_sha256 == value.reference.artifact_sha256
                                  and s.source_document == value.reference.source_document]
                if len(source_options) != 1:
                    raise ValueError("reconstructed document has no exact unique source")
                source, target = source_options[0], dict(evidence_key=value.key)
                raw_size = len(history._contract3_json(value))
                charge_key = (kind, value.key)
            else:
                source = value.source if kind == "cf" else value.request.source.captured_source
                # Actual artifact digest supplies the same fixed64-byte hex key width.
                target = dict(observation_key=source.artifact_sha256)
                raw = asdict(value) if kind == "cf" else dict(request=asdict(value.request),
                    lexical_text=value.lexical_text, decoded_text=value.decoded_text)
                raw_size = 2048 + 2 * len(history._contract3_json(raw))
                charge_key = (kind, hashlib.sha256(history._contract3_json(raw)).hexdigest())
            witness_size = len(history._contract3_json(dict(ordinal=299, association_status="DECLARED_NOT_VERIFIED",
                role=role, source=dict(source_item_id=source.source_item_id, source_observation_id=source.source_observation_id,
                                      occurrence_id=source.occurrence_id), **target)))
            if witness_size > 4096:
                raise ValueError("reconstructed witness record exceeds4KiB before factories")
            node_charge += witness_size + 1
            if charge_key not in charged:
                charged.add(charge_key)
                reservation += raw_size + 1
        if node_charge > 65536:
            raise ValueError("reconstructed output record exceeds64KiB before factories")
        reservation += node_charge + 1
        if reservation > 8 * 1024 * 1024:
            raise ValueError("reconstructed record frontier exceeds8MiB before factories")
    observations, nodes, coverage = {}, [], []
    def source_ref(source):
        return BundleSource(source_item_id=source.source_item_id, source_observation_id=source.source_observation_id,
                            occurrence_id=source.occurrence_id)
    for view, family, year, fact, reason, supported in specifications:
        witnesses, witness_keys = [], set()
        for kind, value, role in supported:
            if kind == "document":
                sources = [source for source in known if source.artifact_sha256 == value.reference.artifact_sha256
                           and source.source_document == value.reference.source_document]
                if len(sources) != 1:
                    raise ValueError("reconstructed document has no exact unique source")
                source, target = sources[0], dict(evidence_key=value.key)
            else:
                observed = (_raw_observation(value, known, cik=cik, original_facts=original_facts,
                    per_share=value.unit == "shares" or value.unit.endswith("/shares"))
                    if kind == "cf" else history._native_observation(value, cik))
                if observed.key in observations and observations[observed.key] != observed:
                    raise ValueError("reconstructed raw occurrence projection conflict")
                observations[observed.key] = observed
                source = value.source if kind == "cf" else value.request.source.captured_source
                target = dict(observation_key=observed.key)
            identity = (role, tuple(target.items()), source)
            if identity in witness_keys:
                continue
            witness_keys.add(identity)
            witnesses.append(history.SourceWitness(ordinal=len(witnesses), association_status="DECLARED_NOT_VERIFIED",
                                                    role=role, source=source_ref(source), **target))
        unit = Unit(status="UNKNOWN") if fact is None else _unit(fact.provenance.unit)
        period = Period(kind="UNKNOWN", origin="UNKNOWN") if fact is None else _period(
            fact.provenance.period_start.isoformat() if fact.provenance.period_start else None,
            fact.provenance.period_end.isoformat() if fact.provenance.period_end else None)
        nodes.append(history.FigureSelection(node_key=f"{view}:{family}.{year}", metric_family=family,
            metric_path=f"{family}.{year}", view=view, state="NOT_ASSESSED", reason=reason, unit=unit,
            period=period, scope=SCOPE, security_basis="NOT_ASSESSED", source_witnesses=tuple(witnesses),
            share_basis=UNKNOWN_BASIS if family in {"annual_eps", "annual_share_counts"} else None))
    for view, _ in views:
        for family in FAMILIES:
            coverage.append(Coverage(metric_family=family, view=view, assessment="PARTIAL",
                metric_paths=tuple(n.metric_path for n in nodes if n.view == view and n.metric_family == family) or (family,),
                reason="NATIVE_SELECTED_SUPPORT_UNQUALIFIED", source_fidelity="UNKNOWN", earlier_history="CAPTURED_ONLY"))
    return dict(observations=tuple(observations.values()), selections=tuple(nodes), coverage=tuple(coverage),
                sources=tuple(source_ref(source) for source in captured_sources),
                document_evidence=tuple(documents[key] for key in sorted(documents)), operand_gaps=tuple(operand_gaps))
