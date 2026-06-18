"""
PostgreSQL compatibility test for CompressionRepository.

Usage:
  DATABASE_URL="postgresql://user:pass@host/db" python3 test_pg.py
  python3 test_pg.py          # runs against SQLite if DATABASE_URL not set
"""

import os, sys, traceback

sys.path.insert(0, os.path.dirname(__file__))

from api import CompressionRepository, DB_PATH

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def run(label, fn):
    try:
        fn()
        print(f"  {PASS}  {label}")
        return True
    except Exception as e:
        print(f"  {FAIL}  {label}")
        traceback.print_exc()
        return False


def main():
    url = os.getenv("DATABASE_URL", "")
    mode = f"PostgreSQL ({url[:30]}...)" if url else "SQLite (no DATABASE_URL set)"
    print(f"\nTesting CompressionRepository — {mode}\n")

    repo = CompressionRepository(DB_PATH)

    results = []

    # 1. Schema init
    results.append(run("init_schema()", lambda: repo.init_schema()))

    # 2. log_event
    results.append(run("log_event()", lambda: repo.log_event(
        api_key="test_key_1",
        original_tokens=500,
        compressed_tokens=350,
        pct_saved=30,
        mode="standard",
        platform="test",
    )))

    # 3. get_stats — verify returns dict with expected keys
    def _test_stats():
        s = repo.get_stats()
        assert "total_compressions" in s, f"missing total_compressions: {s}"
        assert "total_tokens_saved" in s, f"missing total_tokens_saved: {s}"
        assert "avg_compression_pct" in s, f"missing avg_compression_pct: {s}"
        assert s["total_compressions"] >= 1, f"expected >=1 compressions, got {s}"
        print(f"         stats={s}")
    results.append(run("get_stats()", _test_stats))

    # 4. log_feedback
    results.append(run("log_feedback()", lambda: repo.log_feedback(
        original="The quick brown fox",
        compressed="quick brown fox",
        rating=4,
        comment="good",
    )))

    # 5. log_context_event
    results.append(run("log_context_event()", lambda: repo.log_context_event(
        api_key="test_key_1",
        mode="kv_sandwich",
        original_tokens=1000,
        optimized_tokens=780,
        tokens_saved=220,
        messages_total=12,
        messages_pruned=4,
        summary_tokens=80,
        platform="proxy",
    )))

    # 6. log_mcp_event
    results.append(run("log_mcp_event()", lambda: repo.log_mcp_event(
        api_key="test_key_1",
        tool_name="compress_prompt",
        tier="standard",
        tool_session_id="sess_mcp_001",
        original_tokens=200,
        compressed_tokens=140,
        pct_saved=30,
        cache_hit=False,
        claude_session_id=None,
    )))

    # 7. upsert_mcp_tool_session — first turn
    results.append(run("upsert_mcp_tool_session(first turn)", lambda: repo.upsert_mcp_tool_session(
        session_id="sess_mcp_001",
        api_key="test_key_1",
        tool_names=["compress_prompt", "compression_stats"],
        raw_tokens=200,
        dsl_tokens=140,
        tokens_saved=60,
        is_first_turn=True,
    )))
    # second turn (update path)
    results.append(run("upsert_mcp_tool_session(second turn)", lambda: repo.upsert_mcp_tool_session(
        session_id="sess_mcp_001",
        api_key="test_key_1",
        tool_names=["compress_prompt", "compression_stats"],
        raw_tokens=200,
        dsl_tokens=145,
        tokens_saved=55,
        is_first_turn=False,
    )))

    # 8. log_website_event — pageview
    results.append(run("log_website_event(pageview)", lambda: repo.log_website_event(
        session_id="web_sess_001",
        page="/pricing.html",
        event_type="pageview",
        user_id=None,
        country="IT",
        region="Lombardia",
        referrer="direct",
        device_type="desktop",
    )))

    # 9. log_website_event — click
    results.append(run("log_website_event(click)", lambda: repo.log_website_event(
        session_id="web_sess_001",
        page="/pricing.html",
        event_type="click",
        element="plan_solo_cta",
        country="IT",
        region="Lombardia",
        device_type="desktop",
    )))

    # 10. log_website_event — time_on_page
    results.append(run("log_website_event(time_on_page)", lambda: repo.log_website_event(
        session_id="web_sess_001",
        page="/pricing.html",
        event_type="time_on_page",
        duration_sec=47,
        country="IT",
        region="Lombardia",
        device_type="desktop",
    )))

    # 11. get_website_stats
    def _test_web_stats():
        s = repo.get_website_stats(days=30)
        assert "total_pageviews"  in s, f"missing key: {s}"
        assert "unique_sessions"  in s
        assert "top_pages"        in s
        assert "top_countries"    in s
        assert s["total_pageviews"] >= 1
        print(f"         web_stats={s}")
    results.append(run("get_website_stats()", _test_web_stats))

    passed = sum(results)
    total  = len(results)
    print(f"\n{'='*45}")
    print(f"  {passed}/{total} passed")
    if passed < total:
        print("  Some tests failed — check output above.")
        sys.exit(1)
    else:
        print("  All clear.")
    print()


if __name__ == "__main__":
    main()
