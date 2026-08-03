import sqlite3

import pytest


def test_is_job_applied_false_before_add(tmp_db):
    assert tmp_db.is_job_applied("job-1") is False


def test_is_job_applied_true_after_add(tmp_db):
    tmp_db.add_applied_job("job-1", "Title", "https://hh.ru/vacancy/1")
    assert tmp_db.is_job_applied("job-1") is True


def test_add_applied_job_duplicate_id_raises(tmp_db):
    """Дедуп держится на том, что вызывающий код (hh_client) сам проверяет
    is_job_applied ДО add_applied_job — сама таблица дублирование не
    допускает вовсе, а падает. Это фиксирует контракт, а не баг: тихая
    перезапись задним числом была бы хуже явного исключения."""
    tmp_db.add_applied_job("job-1", "Title", "https://hh.ru/vacancy/1")
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.add_applied_job("job-1", "Other title", "https://hh.ru/vacancy/1")


def test_bump_failed_response_increments(tmp_db):
    assert tmp_db.bump_failed_response("job-1", "Title") == 1
    assert tmp_db.bump_failed_response("job-1", "Title") == 2


def test_bump_failed_response_independent_ids(tmp_db):
    tmp_db.bump_failed_response("job-1", "Title 1")
    assert tmp_db.bump_failed_response("job-2", "Title 2") == 1


def test_verdict_cache_missing_returns_none(tmp_db):
    assert tmp_db.get_cached_verdict("nonexistent-hash") is None


def test_verdict_cache_roundtrip(tmp_db):
    tmp_db.set_cached_verdict("hash-1", "YES")
    assert tmp_db.get_cached_verdict("hash-1") == "YES"


def test_verdict_cache_overwrite(tmp_db):
    tmp_db.set_cached_verdict("hash-1", "YES")
    tmp_db.set_cached_verdict("hash-1", "NO")
    assert tmp_db.get_cached_verdict("hash-1") == "NO"


def test_seen_skips_empty_by_default(tmp_db):
    assert tmp_db.load_seen_skips() == set()


def test_seen_skips_mark_and_load(tmp_db):
    tmp_db.mark_skip_seen("job-1")
    tmp_db.mark_skip_seen("job-2")
    assert tmp_db.load_seen_skips() == {"job-1", "job-2"}


def test_seen_skips_mark_twice_does_not_raise(tmp_db):
    tmp_db.mark_skip_seen("job-1")
    tmp_db.mark_skip_seen("job-1")  # INSERT OR IGNORE — не должно падать
    assert tmp_db.load_seen_skips() == {"job-1"}


def test_bump_stat_accumulates(tmp_db):
    tmp_db.bump_stat("applied")
    tmp_db.bump_stat("applied", 2)
    totals = tmp_db.load_stats_totals()
    assert totals["applied"] == 3


def test_load_applied_jobs_empty(tmp_db):
    assert tmp_db.load_applied_jobs() == []


def test_load_applied_jobs_returns_title_and_url_newest_first(tmp_db):
    tmp_db.add_applied_job("job-1", "Тестировщик", "https://hh.ru/vacancy/1")
    tmp_db.add_applied_job("job-2", "QA Engineer", "https://hh.ru/vacancy/2")

    jobs = tmp_db.load_applied_jobs()

    assert [j["id"] for j in jobs] == ["job-2", "job-1"]
    assert jobs[0]["title"] == "QA Engineer"
    assert jobs[0]["url"] == "https://hh.ru/vacancy/2"
    assert jobs[0]["applied_at"]


def test_load_applied_jobs_respects_limit(tmp_db):
    for i in range(5):
        tmp_db.add_applied_job(f"job-{i}", f"Title {i}", f"https://hh.ru/vacancy/{i}")

    assert len(tmp_db.load_applied_jobs(limit=2)) == 2


def test_bump_stat_fields_independent(tmp_db):
    tmp_db.bump_stat("applied", 5)
    tmp_db.bump_stat("skipped", 1)
    totals = tmp_db.load_stats_totals()
    assert totals["applied"] == 5
    assert totals["skipped"] == 1
