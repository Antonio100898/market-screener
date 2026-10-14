"""Real SQL query acceptance; fixtures do not prove publication/migration readiness."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from screener import api, company_query_api
from screener.company_projection import project_companies
from screener.company_queries import CompanyQuery, CompanyQueryRepository, InvalidQuery, QueryUnavailable, RestartQuery
from screener.company_query_schema import QUERY_COLLATION, release_company_projection as p
from screener.durable_jobs import DurableJobRepository
from screener.postgres import (active_data_release, data_release, evidence_artifact, release_company,
                              release_company_lineage, release_company_source)
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from screener.shared_companies import SharedCompanyRepository
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires real local PostgreSQL")
NOW = datetime(2026, 10, 4, 10, tzinfo=timezone.utc)
SECRET = b"query-storage-test-stable-secret-123456"


@pytest.fixture
def storage(monkeypatch):
    settings = StorageSettings.from_env()
    name = f"ms_query_{uuid4().hex}"
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    url = make_url(settings.database_url).set(database=name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url)
    try:
        monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        command.upgrade(config, "head")
        # Migrations own this schema; direct fixture rows prove queries, not publication.
        job = DurableJobRepository(engine).enqueue_scheduled(schedule_key="query-fixture", scheduled_for=NOW,
                                                            kind="query-fixture", parameters={}, due_at=NOW)
        digest = hashlib.sha256(b"query fixture input bundle").hexdigest()
        with engine.begin() as connection:
            connection.execute(evidence_artifact.insert().values(content_sha256=digest, object_key=f"fixture/{digest}",
                                                                  byte_size=26, media_type="application/json", verified_at=NOW))
        rows = _rows()
        companies = SharedCompanyRepository(engine)
        selected = []
        for index, row in enumerate(rows, start=1):
            selected.append(companies.store_and_select(issuer_source="SEC", issuer_identifier=str(index),
                            security_identifier=f"sec:{index:010}:primary", ticker=row["ticker"], security_title="Common Stock",
                            source_accession="query-fixture", security_basis="PRIMARY_COMMON_SHARE", engine_revision=181,
                            canonical_payload={"ticker": row["ticker"], "query_fixture": True}))

        def publish(rows, *, parent=None, complete=True, canonical_only=False, sources=None):
            projections = project_companies(rows)
            with engine.begin() as connection:
                release = connection.scalar(data_release.insert().values(request_key=uuid4().hex, parent_release_id=parent,
                            publishing_job_id=job.job_id, projection_revision=None if canonical_only else 1,
                            input_bundle_sha256=None if canonical_only else digest).returning(data_release.c.release_id))
                for index, (row, projection, company) in enumerate(zip(rows, projections, selected)):
                    connection.execute(release_company.insert().values(release_id=release, security_id=company.security_id, snapshot_id=company.snapshot_id))
                    dependency = (sources or {}).get(company.security_id)
                    connection.execute(release_company_lineage.insert().values(release_id=release, security_id=company.security_id,
                        assessment="COMPLETE" if dependency else "NOT_ASSESSED"))
                    if dependency:
                        connection.execute(release_company_source.insert().values(release_id=release, security_id=company.security_id,
                            source_observation_id=dependency))
                    if canonical_only or not complete and index == len(rows) - 1:
                        continue
                    enriched = {**row, **projection}
                    assumed = {**enriched, "assumption_mode": {"status": "APPLIED", "requested": True, "applied": ["debt"]}}
                    connection.execute(p.insert().values(release_id=release, security_id=company.security_id,
                        projection_revision=1, enriched_payload=enriched, assumed_payload=assumed if index == 0 else None,
                        assumption_status="APPLIED" if index == 0 else "UNAVAILABLE", input_bundle_sha256=digest, universe_ordinal=index,
                        ticker=row["ticker"], name=row.get("name") or "", sector=row.get("sector"), exchange=row.get("exchange"),
                        profile=projection["profile"], search_hay=projection["hay"], index_memberships=projection["idx"],
                        enterprising_verdict=row.get("alignment", {}).get("enterprising", {}).get("verdict"),
                        defensive_verdict=row.get("alignment", {}).get("defensive", {}).get("verdict"),
                        enterprising_gap=row.get("fixture_gap", "OUT_OF_SCOPE"), criteria_passed=row["n_pass"],
                        positive_eps_years=(projection["eps10"] or {}).get("positive"), unjudged=projection["unjudged"],
                        inapplicable=projection["inapplicable"], net_net=projection["netNet"], market_cap=projection["mcap"],
                        price=row["price"], off_high=row.get("price_stats", {}).get("pct_below_52w_high"), pe=projection["pe"], pe3=projection["pe3"],
                        dividend_yield=projection["dividendYield"], return_quality=projection["returnQuality"]["score"]))
                connection.execute(update(data_release).where(data_release.c.release_id == release).values(published_at=NOW))
                connection.execute(update(active_data_release).where(active_data_release.c.singleton_id == 1).values(release_id=release))
            return release

        release = publish(rows)
        yield engine, CompanyQueryRepository(engine, cursor_secret=SECRET), release, rows, selected, publish
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def _rows():
    rows = []
    for ticker, name, sector, pe in (("E", "Zulu", None, 5), ("A", "é", None, None), ("D", "Alpha", "Financials", 0),
                                   ("B", "e\u0301", "Energy", -2), ("C", "Alpha", "Energy", 10), ("F", "100%_literal", "Energy", 15)):
        rows.append({"ticker": ticker, "name": name, "sector": sector, "exchange": "NYSE", "price": 30, "shares": 100,
                     "price_asof": "2026-01-01", "ncavps": 30, "annual_eps": {"2023": 5, "2024": 5, "2025": 5},
                     "criteria": [{"n": 7, "status": "PASS", "value": 1}, {"n": 1, "status": "PASS", "value": pe}],
                     "n_pass": 6 if ticker == "E" else 3, "graham_profile": "OPERATING", "fixture_gap": "ONE_GAP",
                     "alignment": {"enterprising": {"verdict": "ALIGNED"}, "defensive": {"verdict": "BLOCKED"}},
                     "index_memberships": ["S&P 500"] if ticker in {"A", "C"} else ["DJIA"] if ticker == "B" else []})
    rows[1]["criteria"][0]["status"] = "NOT_APPLICABLE"
    rows[2]["alignment"] = {}
    return rows


def _tickers(result):
    return [row["ticker"] for row in result["rows"]]


def test_actual_nondeterministic_collation_matches_js_ties(storage):
    engine, repository, release, *_ = storage
    with engine.connect() as connection:
        assert connection.scalar(text(f"select U&'\\00E9' collate \"{QUERY_COLLATION}\" = U&'e\\0301' collate \"{QUERY_COLLATION}\""))
    assert _tickers(repository.list(CompanyQuery(sorts=(("name", 1),)), release_id=release)) == ["F", "C", "D", "A", "B", "E"]
    query = CompanyQuery(sorts=(("name", 1),), limit=4)
    first = repository.list(query, release_id=release)
    assert _tickers(first) == ["F", "C", "D", "A"]
    assert _tickers(repository.list(query, cursor=first["next_cursor"])) == ["B", "E"]


@pytest.mark.parametrize("stored_text", ["A" * 1001, "A" + "漢" * 5000], ids=["ascii-1001", "multibyte-5001"])
def test_long_projection_name_and_sector_round_trip_bounded_cursors(storage, stored_text):
    _, repository, first, rows, _, publish = storage
    rows[0]["name"] = stored_text
    rows[0]["sector"] = stored_text
    release = publish(rows, parent=first)
    for sort_key in ("name", "sector"):
        query = CompanyQuery(sorts=((sort_key, 1),), limit=2)
        expected = _tickers(repository.list(CompanyQuery(sorts=query.sorts), release_id=release))
        cursor, found, token_lengths = None, [], []
        while True:
            result = repository.list(query, release_id=release, cursor=cursor)
            found.extend(_tickers(result))
            cursor = result["next_cursor"]
            if cursor is None:
                break
            assert len(cursor) <= 8192
            token_lengths.append(len(cursor))
        assert found == expected
        assert max(token_lengths) < 512
    assert repository.detail(ticker="E", release_id=release, assume_absent_zero=False)["row"]["name"] == stored_text


def test_categorical_identity_remains_exact_while_text_order_ties(storage):
    _, repository, first, rows, _, publish = storage
    rows[0]["sector"] = "é"
    rows[1]["sector"] = "e\u0301"
    release = publish(rows, parent=first)
    assert _tickers(repository.list(CompanyQuery(sectors=("é",)), release_id=release)) == ["E"]
    sectors = dict(repository.facets(release_id=release)["sectors"])
    assert sectors["é"] == sectors["e\u0301"] == 1


def test_nulls_last_both_directions_and_compound_cursor_counts(storage):
    _, repository, release, *_ = storage
    assert _tickers(repository.list(CompanyQuery(sorts=(("pe", 1),)), release_id=release)) == ["B", "D", "E", "C", "F", "A"]
    assert _tickers(repository.list(CompanyQuery(sorts=(("pe", -1),)), release_id=release)) == ["F", "C", "E", "D", "B", "A"]
    query = CompanyQuery(sorts=(("sector", 1), ("pe", -1)), limit=2)
    expected = ["F", "C", "B", "D", "E", "A"]
    cursor, found = None, []
    while True:
        page = repository.list(query, release_id=release, cursor=cursor)
        assert page["matched"] == 6 and page["counts"]["met_3"] == 6
        found.extend(_tickers(page))
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert found == expected


def test_positive_bounds_search_filtered_counts_and_global_facets(storage):
    _, repository, release, *_ = storage
    page = repository.list(CompanyQuery(max_pe=10, min_positive_eps=3, limit=1), release_id=release)
    assert _tickers(page) == ["C"] and page["matched"] == 2 and page["counts"]["met_6"] == 1
    assert _tickers(repository.list(CompanyQuery(q="%_literal"), release_id=release)) == ["F"]
    empty = repository.list(CompanyQuery(q="Alpha", sectors=("Absent",)), release_id=release)
    assert empty["matched"] == 0 and empty["matches_anywhere"] == 2
    assert _tickers(repository.list(CompanyQuery(indexes=("Outside", "DJIA")), release_id=release)) == ["B", "D", "E", "F"]
    facets = repository.facets(release_id=release)
    assert facets["sectors"] == [["Energy", 3], ["Unclassified", 2], ["Financials", 1]]
    assert facets["indexes"] == [["DJIA", 1], ["S&P 500", 2], ["Outside", 3]]
    overview = repository.overview(release_id=release)
    assert overview["members"] == 2 and overview["pe"] == {"median": 10, "count": 1}
    assert overview["pe3"] == {"median": 6, "count": 2}


def test_missing_lens_alignment_and_dormant_gap_semantics(storage):
    _, repository, release, *_ = storage
    assert repository.list(CompanyQuery(lens="ENTERPRISING"), release_id=release)["matched"] == 6
    assert repository.list(CompanyQuery(lens="ENTERPRISING", fit="ALIGNED"), release_id=release)["matched"] == 5
    assert repository.list(CompanyQuery(lens="BOTH", gaps="TWO_GAPS"), release_id=release)["matched"] == 6
    assert repository.list(CompanyQuery(lens="ENTERPRISING", gaps="TWO_GAPS"), release_id=release)["matched"] == 0


def test_cursor_pins_release_across_activation_and_rejects_changed_query(storage):
    _, repository, first, rows, selected, publish = storage
    query = CompanyQuery(limit=2)
    first_page = repository.list(query, release_id=first)
    rows[0]["name"] = "Changed"
    second = publish(rows, parent=first)
    page = repository.list(query, cursor=first_page["next_cursor"])
    assert page["release"]["release_id"] == first and _tickers(page) == ["C", "D"]
    assert repository.detail(security_id=selected[0].security_id, release_id=first, assume_absent_zero=False)["row"]["name"] == "Zulu"
    assert repository.detail(ticker="e", release_id=second, assume_absent_zero=False)["row"]["name"] == "Changed"
    with pytest.raises(InvalidQuery):
        repository.list(CompanyQuery(limit=2, max_pe=10), cursor=first_page["next_cursor"])
    with pytest.raises(InvalidQuery):
        repository.list(query, release_id=second, cursor=first_page["next_cursor"])
    with pytest.raises(RestartQuery):
        repository.list(query, release_id=999)
    identity = {"contract": 1, "projection": 1, "release": first, "query": query.fingerprint(),
                "validity_generation": first_page["source_validity"]["validity_generation"]}
    for anchor in (None, True, 0, 2**63, "1", 999999):
        with pytest.raises(InvalidQuery):
            repository.list(query, cursor=repository.cursors.encode({**identity, "anchor": anchor}))
    filtered = CompanyQuery(q="Alpha", limit=2)
    with pytest.raises(InvalidQuery, match="absent from its release query"):
        repository.list(filtered, cursor=repository.cursors.encode({**identity, "query": filtered.fingerprint(),
                                                                   "anchor": selected[0].security_id}))


def test_detail_defaults_disclosed_assumptions_and_api_no_fallback(storage, monkeypatch):
    _, repository, release, _, selected, _ = storage
    assert repository.detail(security_id=selected[0].security_id, release_id=release)["row"]["assumption_mode"]["status"] == "APPLIED"
    assert "assumption_mode" not in repository.detail(security_id=selected[0].security_id, release_id=release, assume_absent_zero=False)["row"]
    fallback = repository.detail(ticker="a", release_id=release)["row"]
    assert fallback["assumption_mode"]["status"] == "UNAVAILABLE" and fallback["criteria"][0]["status"] == "NOT_APPLICABLE"
    monkeypatch.setattr(company_query_api, "query_repository", lambda: repository)
    client = TestClient(api.app)
    assert client.get(f"/api/v1/companies/by-ticker/A?release={release}&assume_absent_zero=false").status_code == 200
    assert client.get(f"/api/v1/companies/by-ticker/AA?release={release}").status_code == 404
    assert client.get("/api/v1/companies?tracked_only=true").status_code == 400
    status = client.get("/api/v1/data-status").json()
    assert status["projection_ready"] and status["validity_status"] == "NOT_ASSESSED"
    identities = client.get(f"/api/v1/companies?release={release}&row_fields=identity&limit=2").json()
    assert [row["ticker"] for row in identities["rows"]] == ["A", "B"]
    assert all(set(row) == {"security_id", "ticker", "name"} for row in identities["rows"])
    assert identities["next_cursor"]
    assert all(row["status"] == "NOT_ASSESSED" for row in identities["source_validity"]["companies"])


def test_mounted_api_pins_detail_and_reports_projection_and_config_unready(storage, monkeypatch):
    engine, repository, first, rows, _, publish = storage
    monkeypatch.setattr(company_query_api, "query_repository", lambda: repository)
    client = TestClient(api.app)
    pinned_url = f"/api/v1/companies/by-ticker/e?release={first}"
    before = client.get(pinned_url).json()
    assert before["row"]["assumption_mode"]["status"] == "APPLIED"
    rows[0]["name"] = "Later release"
    second = publish(rows, parent=first)
    assert client.get(pinned_url).json() == before
    strict = client.get(pinned_url + "&assume_absent_zero=false").json()
    assert strict["release"]["release_id"] == first and strict["row"]["name"] == "Zulu"
    assert "assumption_mode" not in strict["row"]
    assert client.get("/api/v1/companies/by-ticker/e").json()["release"]["release_id"] == second
    unsupported = publish(rows, parent=second, canonical_only=True)
    response = client.get(f"/api/v1/companies?release={unsupported}")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "QUERY_NOT_READY"
    assert not client.get("/api/v1/data-status").json()["projection_ready"]
    monkeypatch.setattr(company_query_api, "query_repository", lambda: CompanyQueryRepository(engine, cursor_secret=b""))
    response = client.get(f"/api/v1/companies?release={first}")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "QUERY_NOT_READY"
    assert not client.get("/api/v1/data-status").json()["query_configuration_ready"]


def test_incomplete_projection_is_not_query_ready(storage):
    _, repository, first, rows, _, publish = storage
    with pytest.raises(IntegrityError, match="complete supported projection coverage"):
        publish(rows, parent=first, complete=False)
    assert repository.status()["projection_ready"]
    incomplete = publish(rows, parent=first, canonical_only=True)
    with pytest.raises(QueryUnavailable):
        repository.list(CompanyQuery(), release_id=incomplete)
    assert not repository.status()["projection_ready"]


@pytest.fixture
def witnessed(storage):
    engine, repository, parent, rows, selected, publish = storage
    jobs = DurableJobRepository(engine)
    jobs.enqueue_scheduled(schedule_key="query-validity", scheduled_for=NOW, kind="query-validity",
                           parameters={}, due_at=NOW)
    lease = jobs.claim_next(owner="query-validity-test", lease_duration=timedelta(minutes=5), now=NOW,
                            allowed_kinds=("query-validity",)).lease
    observations = SourceObservationRepository(engine, clock=lambda: NOW)
    with engine.connect() as connection:
        artifact = connection.scalar(select(evidence_artifact.c.content_sha256))

    def record(key, *, state="present", second=0, reason=None):
        return observations.record(lease, item=SourceItemIdentity("SEC", "resource", "query-input", "0000000002"),
            item_outcome_key=key, state=state, metadata={"role": "query_fixture", **({"reason": reason} if reason else {})},
            source_url="https://www.sec.gov/query-fixture", artifact_sha256=artifact if state == "present" else None,
            detected_at=NOW + timedelta(seconds=second))

    captured = record("captured")
    release = publish(rows, parent=parent, sources={selected[1].security_id: captured.observation.source_observation_id})
    # Genuine source repository transactions over synthetic artifacts prove status/SQL semantics only.
    return engine, repository, release, selected, record


def test_two_clients_changed_source_restarts_paging_unchanged_check_keeps_cursor(witnessed, monkeypatch):
    engine, repository, release, selected, record = witnessed
    monkeypatch.setattr(company_query_api, "query_repository", lambda: repository)
    first_client, second_client = TestClient(api.app), TestClient(api.app)
    first = first_client.get(f"/api/v1/companies?release={release}&limit=2&row_fields=identity").json()
    generation = first["source_validity"]["validity_generation"]
    matched = first["source_validity"]["companies"][0]
    assert matched["security_id"] == selected[1].security_id and matched["status"] == "CAPTURED_MATCH"
    assert first["source_validity"]["companies"][1]["status"] == "NOT_ASSESSED"
    detail_before = second_client.get(f"/api/v1/companies/by-ticker/A?release={release}&assume_absent_zero=false").json()
    record("same-content", second=1)
    url = f'/api/v1/companies?release={release}&limit=2&row_fields=identity&cursor={first["next_cursor"]}'
    assert first_client.get(url).status_code == 200
    fresh = second_client.get(f"/api/v1/companies/by-ticker/A?release={release}&assume_absent_zero=false").json()
    assert fresh["source_validity"]["validity_generation"] == generation
    assert fresh["source_validity"]["companies"][0]["dependencies"][0]["checked_at"] != matched["dependencies"][0]["checked_at"]
    record("pending", state="pending", second=2, reason="not_found")
    restart = first_client.get(url)
    assert restart.status_code == 409 and restart.json()["detail"]["code"] == "RESTART_QUERY"
    after = second_client.get(f"/api/v1/companies/by-ticker/A?release={release}&assume_absent_zero=false").json()
    assert after["row"] == detail_before["row"] and after["release"] == detail_before["release"]
    assert after["source_validity"]["validity_generation"] == generation + 1
    assert after["source_validity"]["companies"][0]["source_statuses"] == ["INPUTS_PENDING"]
    assert after["source_validity"]["official_financial_readiness"] == "NOT_ASSESSED"
    assert second_client.get(f"/api/v1/companies/999999?release={release}").status_code == 404
    empty = first_client.get(f"/api/v1/companies?release={release}&q=absent-search-fixture").json()
    assert empty["source_validity"]["companies"] == [] and empty["matched"] == 0
    status = second_client.get("/api/v1/data-status").json()
    assert status["lineage_counts"] == {"complete": 1, "not_assessed": 5}
    assert status["validity_generation"] == generation + 1 and status["validity_status"] == "NOT_ASSESSED"
    assert not status["newest_filings_assessed"] and not status["numeric_invalidation_assessed"]


def test_source_commit_between_counts_and_page_is_one_repeatable_read_snapshot(witnessed, monkeypatch):
    _, repository, release, _, record = witnessed
    before = repository.list(CompanyQuery(limit=2), release_id=release)
    original = repository._counts
    committed = False

    def counts(connection, base):
        nonlocal committed
        assert connection.get_isolation_level() == "REPEATABLE READ"
        result = original(connection, base)
        if not committed:
            record("racing-pending", state="pending", second=1, reason="not_found")
            committed = True
        return result

    monkeypatch.setattr(repository, "_counts", counts)
    pinned = repository.list(CompanyQuery(limit=2), release_id=release)
    assert pinned == before
    fresh = repository.list(CompanyQuery(limit=2), release_id=release)
    assert fresh["rows"] == before["rows"] and fresh["counts"] == before["counts"]
    assert fresh["source_validity"]["validity_generation"] == before["source_validity"]["validity_generation"] + 1
    assert fresh["source_validity"]["companies"][0]["source_statuses"] == ["INPUTS_PENDING"]
    with pytest.raises(RestartQuery, match="source state changed"):
        repository.list(CompanyQuery(limit=2), cursor=pinned["next_cursor"])


@pytest.mark.parametrize("operation", ["detail", "status", "facets", "overview"])
def test_nonpaged_reads_keep_source_commit_and_generation_coherent(witnessed, monkeypatch, operation):
    _, repository, release, _, record = witnessed

    def read():
        if operation == "detail":
            return repository.detail(ticker="A", release_id=release, assume_absent_zero=False)
        return getattr(repository, operation)(**({} if operation == "status" else {"release_id": release}))

    before = read()
    original, committed = repository._generation, False

    def generation(connection):
        nonlocal committed
        assert connection.get_isolation_level() == "REPEATABLE READ"
        value = original(connection)
        if not committed:
            record("racing-change", state="pending", second=1, reason="not_found")
            committed = True
        return value

    monkeypatch.setattr(repository, "_generation", generation)
    assert read() == before
    fresh = read()
    if operation == "detail":
        assert fresh["row"] == before["row"]
        assert fresh["source_validity"]["validity_generation"] == before["source_validity"]["validity_generation"] + 1
        assert fresh["source_validity"]["companies"][0]["source_statuses"] == ["INPUTS_PENDING"]
    else:
        assert fresh["validity_generation"] == before["validity_generation"] + 1


def test_old_or_invalid_generation_cursor_restarts_and_bootstrap_status_stays_unknown(storage, monkeypatch):
    _, repository, release, _, _, _ = storage
    monkeypatch.setattr(company_query_api, "query_repository", lambda: repository)
    client = TestClient(api.app)
    page = client.get(f"/api/v1/companies?release={release}&limit=2").json()
    identity = repository.cursors.decode(page["next_cursor"])
    for generation in (None, True, "0", -1):
        token = repository.cursors.encode({**identity, "validity_generation": generation})
        response = client.get(f"/api/v1/companies?release={release}&limit=2&cursor={token}")
        assert response.status_code == 409 and response.json()["detail"]["code"] == "RESTART_QUERY"
    identity.pop("validity_generation")
    response = client.get(f'/api/v1/companies?release={release}&limit=2&cursor={repository.cursors.encode(identity)}')
    assert response.status_code == 409
    status = client.get("/api/v1/data-status").json()
    assert status["projection_ready"] and status["lineage_counts"] == {"complete": 0, "not_assessed": 6}
    assert status["validity_status"] == status["official_financial_readiness"] == "NOT_ASSESSED"


def test_page_source_assessment_is_bounded_and_has_indexed_sql_path(witnessed, monkeypatch):
    engine, repository, release, _, _ = witnessed
    from screener.source_validity import SourceValidityRepository
    original, seen, statements = SourceValidityRepository.assess_on_connection, [], []

    def assess(connection, release_id, security_ids):
        seen.append(tuple(security_ids))
        assert 1 <= len(security_ids) <= 300
        return original(connection, release_id, security_ids)

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if statement.startswith("SELECT") and "FROM release_company_source" in statement:
            statements.append((statement, parameters))

    monkeypatch.setattr(SourceValidityRepository, "assess_on_connection", staticmethod(assess))
    event.listen(engine, "before_cursor_execute", capture)
    try:
        page = repository.list(CompanyQuery(limit=2, row_fields="identity"), release_id=release)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(seen) == 1 and set(seen[0]) == {row["security_id"] for row in page["rows"]}
    assert len(statements) == 1 and "security_id IN" in statements[0][0]
    with engine.begin() as connection:
        natural_plan = connection.exec_driver_sql("EXPLAIN (FORMAT JSON) " + statements[0][0], statements[0][1]).scalar_one()
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = connection.exec_driver_sql("EXPLAIN (FORMAT JSON) " + statements[0][0], statements[0][1]).scalar_one()
    rendered = json.dumps(plan)
    assert "pk_release_company_source" in rendered and "pk_source_item_current" in rendered
    assert any(name in rendered for name in ("pk_source_observation", "uq_source_observation_item_binding", "uq_source_observation_identity"))
    evidence = {"fixture_rows": 6, "assessed_ids": list(seen[0]), "scope": "bounded small fixture index-capability only",
                "forced_enable_seqscan_off": True, "index_capability_plan": plan, "natural_small_fixture_plan": natural_plan}
    (Path(__file__).parents[3] / "local/query-validity-explain-2026-10-04.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
