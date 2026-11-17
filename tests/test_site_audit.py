"""Show previous website checks missing injected defects, then corrected checks catching them."""

from tests.site_audit import (
    FORBIDDEN_RANKING_WORDS,
    LEGACY_RATE_FIGURE,
    RATE_FIGURE,
    RATE_FIGURE_EXAMPLES,
    automatic_request_urls,
    consent_default_calls,
    csp_blocks_inline_script,
    csp_problems,
    database_findings,
    executable_inline_scripts,
    forbidden_ranking_problems,
    header_request_urls,
    legacy_asset_url_allowed,
    legacy_citation_ok,
    legacy_db_source_clean,
    legacy_enabled_analytics_html_ok,
    publication_problems,
    same_origin,
    source_section_problems,
)


def test_legacy_rate_regex_misses_ordinary_percentages() -> None:
    """The old trailing word-boundary misses 5.25%; the lookahead catches it."""
    for sample in RATE_FIGURE_EXAMPLES:
        for text in (sample, f" {sample} ", f"<p>{sample}</p>", f"Rate {sample}."):
            assert LEGACY_RATE_FIGURE.search(text) is None
            assert RATE_FIGURE.search(text)
    assert RATE_FIGURE.search("section 12 of the guide") is None
    assert RATE_FIGURE.search("CC BY 4.0") is None


def test_legacy_asset_and_csp_checks_miss_injected_requests() -> None:
    """Prefix and substring checks accept lookalikes, protocol-relative URLs, and loose CSP."""
    origin = "https://cu-rates.org"
    lookalike = "https://cu-rates.org.evil.example/tracker.js"
    protocol_relative = "//evil.example/app.js"
    assert legacy_asset_url_allowed(lookalike, origin)
    assert legacy_asset_url_allowed(protocol_relative, origin)
    assert not same_origin(lookalike, origin)
    assert not same_origin(protocol_relative, origin)
    assert same_origin("/static/style.css", origin)
    assert same_origin("https://cu-rates.org/privacy", origin)

    html = "<link rel='stylesheet' href='/static/style.css'>"
    headers = {"Link": '<https://cdn.example/lib.js>; rel="preload"; as="script"'}
    assert "https://" not in html
    assert header_request_urls(headers) == ["https://cdn.example/lib.js"]
    assert publication_problems(html, headers, origin)

    permissive = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https:; "
        "style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    strict = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; font-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    assert "script-src 'self'" in permissive
    assert "default-src 'self'" in permissive
    assert csp_problems(permissive)
    assert csp_problems(strict) == []
    assert csp_blocks_inline_script(strict)
    assert not csp_blocks_inline_script(permissive)


def test_legacy_analytics_strings_miss_behavior() -> None:
    """String presence accepts a blocked or granted consent state and a protocol-relative tag."""
    html = (
        "<p>gtag('consent', 'default' and 'analytics_storage': 'denied'.</p>"
        '<script src="//www.google-analytics.com/analytics.js"></script>'
    )
    assert legacy_enabled_analytics_html_ok(html)
    requested = automatic_request_urls(html)
    assert any(not same_origin(url, "https://cu-rates.org") for url in requested)

    script = (
        "// 'analytics_storage': 'denied'\n"
        "gtag('consent', 'default', {'analytics_storage': 'granted',"
        " 'ad_storage': 'denied', 'ad_user_data': 'denied',"
        " 'ad_personalization': 'denied'});\n"
    )
    assert "'analytics_storage': 'denied'" in script
    assert consent_default_calls(script)[0]["analytics_storage"] == "granted"

    inline = "<script>gtag('consent', 'default', {'analytics_storage': 'denied'});</script>"
    assert "gtag('consent', 'default'" in inline
    assert executable_inline_scripts(inline) == 1
    assert csp_blocks_inline_script("script-src 'self'")


def test_legacy_db_guard_misses_database_name_open_and_transitive_import() -> None:
    """The old text guard ignores cu_rates.db, open(), and a module reached through an import."""
    direct = "import cu_rates.db\nopen('cu_rates.db')\n"
    assert legacy_db_source_clean(direct)
    direct_findings = database_findings({"app": direct}, ["app"])
    assert any("cu_rates.db" in item for item in direct_findings)

    bridge = "from cu_rates.pipeline import slice\n"
    nested = "import sqlite3\n"
    assert legacy_db_source_clean(bridge)
    transitive = database_findings(
        {
            "cu_rates.web.views": bridge,
            "cu_rates.pipeline.slice": nested,
        },
        ["cu_rates.web.views"],
    )
    assert any("sqlite3" in item for item in transitive)


def test_legacy_citation_check_passes_without_external_sources() -> None:
    """Removing every external citation still satisfies the old link count."""
    html = """
    <section class="sources-section">
      <h2>Authoritative Sources and Regulatory References</h2>
      <ul>
        <li>
          <a href="/methodology">Methodology</a>
          <span>unverified - reviewer must confirm the link and the claim</span>
        </li>
      </ul>
    </section>
    """
    assert legacy_citation_ok(html)
    assert source_section_problems(html, "https://cu-rates.org")


def test_positive_rate_examples_are_the_detector_used_for_pages() -> None:
    """Ordinary rate figures are the examples the page scans must reject."""
    for sample in RATE_FIGURE_EXAMPLES:
        assert RATE_FIGURE.fullmatch(sample)


def test_forbidden_ranking_words_guard_detects_ranking_claims() -> None:
    """Guard catches forbidden ranking wording and permits neutral comparison text."""
    for word in FORBIDDEN_RANKING_WORDS:
        sample = f"Discover our {word} offers this month"
        problems = forbidden_ranking_problems(sample)
        assert problems == [f"forbidden ranking word '{word}'"]

    multiple = "We show the best and cheapest top options recommended for you"
    assert forbidden_ranking_problems(multiple) == [
        "forbidden ranking word 'best'",
        "forbidden ranking word 'cheapest'",
        "forbidden ranking word 'recommended'",
        "forbidden ranking word 'top'",
    ]

    clean = "Compare publicly advertised terms sorted by institution name or observed date"
    assert forbidden_ranking_problems(clean) == []

    # Superlatives and ranking phrases in titles, headings, alt text and body
    assert forbidden_ranking_problems("Find the lowest rates today") == [
        "forbidden ranking word 'lowest'"
    ]
    assert forbidden_ranking_problems("<title>The #1 Credit Union in MA</title>") == [
        "forbidden ranking word '#1'"
    ]
    assert forbidden_ranking_problems("<h1>Cheapest mortgage options</h1>") == [
        "forbidden ranking word 'cheapest'"
    ]
    assert forbidden_ranking_problems('<img src="banner.png" alt="our top recommended picks">') == [
        "forbidden ranking word 'recommended'",
        "forbidden ranking word 'top'",
    ]


def test_database_findings_narrows_snapshot_sqlite_to_readonly() -> None:
    """Snapshot module SQLite exception forbids write methods and statements."""
    from tests.site_audit import database_findings

    clean_sources = {
        "cu_rates.web": "import cu_rates.snapshot.reader",
        "cu_rates.snapshot.reader": (
            "import sqlite3\n"
            "conn = sqlite3.connect('file:pub.sqlite?mode=ro')\n"
            "conn.execute('SELECT * FROM info')"
        ),
    }
    assert database_findings(clean_sources, ["cu_rates.web"]) == []

    commit_sources = {
        "cu_rates.web": "import cu_rates.snapshot.reader",
        "cu_rates.snapshot.reader": (
            "import sqlite3\nconn = sqlite3.connect('test')\nconn.commit()"
        ),
    }
    findings = database_findings(commit_sources, ["cu_rates.web"])
    assert any("sqlite write method" in f for f in findings)

    insert_sources = {
        "cu_rates.web": "import cu_rates.snapshot.reader",
        "cu_rates.snapshot.reader": (
            "import sqlite3\n"
            "conn = sqlite3.connect('test')\n"
            "conn.execute('INSERT INTO test VALUES (1)')"
        ),
    }
    findings = database_findings(insert_sources, ["cu_rates.web"])
    assert any("sqlite write statement" in f for f in findings)


def test_forbidden_ranking_words_split_markup_and_entities() -> None:
    """Split markup and entities cannot bypass forbidden ranking word detection."""
    from tests.site_audit import forbidden_ranking_problems

    # Split markup words
    assert "forbidden ranking word 'lowest'" in forbidden_ranking_problems("<b>low</b>est rate")
    assert "forbidden ranking word 'best'" in forbidden_ranking_problems("The <i>be</i>st lender")
    assert "forbidden ranking word '#1'" in forbidden_ranking_problems("Rated <b>#</b>1 CU")

    # Decoded entity checks
    assert "forbidden ranking word '#1'" in forbidden_ranking_problems("We are &#35;1 in MA")
    assert "forbidden ranking word '#1'" in forbidden_ranking_problems("Rated &num;1 overall")
    assert "forbidden ranking word 'lowest'" in forbidden_ranking_problems("low&#101;st rates")

    # Clean text with no forbidden words passes
    assert forbidden_ranking_problems("<p>Compare <b>advertised</b> mortgage terms</p>") == []


def test_database_findings_forbids_variable_writes_mutating_pragmas_and_writable_connections() -> (
    None
):
    """AST database scan catches variable SQL writes, mutating PRAGMAs,
    executescript, and writable connections.
    """
    from tests.site_audit import database_findings

    # Variable-built write statement
    var_write_sources = {
        "cu_rates.web": "import cu_rates.snapshot.reader",
        "cu_rates.snapshot.reader": (
            "import sqlite3\n"
            "conn = sqlite3.connect('file:pub.sqlite?mode=ro')\n"
            "sql = 'INSERT INTO snapshot VALUES (1)'\n"
            "conn.execute(sql)"
        ),
    }
    findings = database_findings(var_write_sources, ["cu_rates.web"])
    assert any("sqlite write statement" in f for f in findings)

    # Mutating PRAGMA
    pragma_sources = {
        "cu_rates.web": "import cu_rates.snapshot.reader",
        "cu_rates.snapshot.reader": (
            "import sqlite3\n"
            "conn = sqlite3.connect('file:pub.sqlite?mode=ro')\n"
            "conn.execute('PRAGMA journal_mode=WAL')"
        ),
    }
    findings = database_findings(pragma_sources, ["cu_rates.web"])
    assert any("sqlite mutating pragma" in f for f in findings)

    # executescript
    execscript_sources = {
        "cu_rates.web": "import cu_rates.snapshot.reader",
        "cu_rates.snapshot.reader": (
            "import sqlite3\n"
            "conn = sqlite3.connect('file:pub.sqlite?mode=ro')\n"
            "conn.executescript('SELECT 1;')"
        ),
    }
    findings = database_findings(execscript_sources, ["cu_rates.web"])
    assert any("sqlite forbidden executescript" in f for f in findings)

    # Writable connection without mode=ro
    writable_sources = {
        "cu_rates.web": "import cu_rates.snapshot.reader",
        "cu_rates.snapshot.reader": (
            "import sqlite3\nconn = sqlite3.connect('pub.sqlite')\nconn.execute('SELECT 1')"
        ),
    }
    findings = database_findings(writable_sources, ["cu_rates.web"])
    assert any("sqlite writable connection" in f for f in findings)
