"""Detectors for website publication guarantees.

Legacy helpers reproduce the previous checks so tests can show an injected defect
slipping through, then the corrected detector catching it.
"""

import ast
import html
import re
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

LEGACY_RATE_FIGURE = re.compile(r"\b\d+(\.\d+)?%\b")
RATE_FIGURE = re.compile(r"\b\d+(?:\.\d+)?%(?!\w)")
RATE_FIGURE_EXAMPLES = ("5.25%", "5%", "6.875%", "10.5%", "0.125%")

UNVERIFIED_CITATION_MARKER = "unverified - reviewer must confirm the link and the claim"
_CONSENT_CALL = re.compile(r"""gtag\(\s*(['"])consent\1\s*,\s*(['"])default\2\s*,\s*\{""")
_CONSENT_UPDATE = re.compile(r"""gtag\(\s*(['"])consent\1\s*,\s*(['"])update\2""")
_CONSENT_PAIR = re.compile(
    r"""(['"]?)(analytics_storage|ad_storage|ad_user_data|ad_personalization)\1"""
    r"""\s*:\s*(['"])([^'"]*)\3"""
)
_JS_URL = re.compile(r"""['"]((?:https?:)?//[^'"]+)['"]""")
_SOURCE_DIRECTIVES = {
    "default-src",
    "script-src",
    "script-src-elem",
    "script-src-attr",
    "style-src",
    "style-src-elem",
    "style-src-attr",
    "img-src",
    "font-src",
    "connect-src",
    "media-src",
    "object-src",
    "frame-src",
    "worker-src",
    "manifest-src",
    "base-uri",
    "form-action",
    "frame-ancestors",
}
_FETCH_DIRECTIVES = ("script-src", "style-src", "img-src", "font-src", "connect-src")
_ALLOWED_SOURCES = {"'self'", "'none'"}
_ASSET_ATTRIBUTES = {
    "script": "src",
    "img": "src",
    "iframe": "src",
    "audio": "src",
    "video": "src",
    "source": "src",
    "embed": "src",
    "track": "src",
    "object": "data",
    "form": "action",
}
_JSON_SCRIPT_TYPES = {"application/ld+json", "application/json"}


def legacy_asset_url_allowed(url: str, origin: str) -> bool:
    """Previous prefix check. Protocol-relative URLs and lookalike hosts pass."""
    if url.startswith("http://") or url.startswith("https://"):
        return url.startswith(origin)
    return True


def legacy_enabled_analytics_html_ok(html: str) -> bool:
    """Previous string check for an enabled analytics page."""
    return (
        "gtag('consent', 'default'" in html
        and "'analytics_storage': 'denied'" in html
        and "googletagmanager.com/gtag/js" not in html
    )


def legacy_db_source_clean(source: str) -> bool:
    """Previous source-text guard. It ignores cu_rates.db and other modules."""
    forbidden = ("sqlite3", "sqlalchemy", "cu_rates.artifact_store")
    return all(term not in source for term in forbidden)


def legacy_citation_ok(html: str) -> bool:
    """Previous citation check. An internal methodology link satisfies it."""
    if UNVERIFIED_CITATION_MARKER not in html:
        return False
    if "Authoritative Sources" not in html and "Regulatory Guidance" not in html:
        return False
    citation_links = [
        href
        for href in anchor_urls(html)
        if href.startswith("http://") or href.startswith("https://") or href == "/methodology"
    ]
    return len(citation_links) >= 1


def strip_js_comments(source: str) -> str:
    """Remove JavaScript comments without dropping quoted text."""
    output: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        pair = source[index : index + 2]
        if pair == "//":
            newline = source.find("\n", index)
            if newline == -1:
                break
            index = newline
            continue
        if pair == "/*":
            end = source.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        character = source[index]
        if character in {"'", '"'}:
            output.append(character)
            index += 1
            while index < length:
                current = source[index]
                output.append(current)
                index += 1
                if current == "\\":
                    if index < length:
                        output.append(source[index])
                        index += 1
                    continue
                if current == character:
                    break
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _object_at(source: str, start: int) -> str:
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    return source[start:]


def consent_default_calls(source: str) -> list[dict[str, str]]:
    """Return each executing consent-default object, ignoring comments."""
    stripped = strip_js_comments(source)
    calls: list[dict[str, str]] = []
    for match in _CONSENT_CALL.finditer(stripped):
        body = _object_at(stripped, match.end() - 1)
        parsed = {item.group(2): item.group(4) for item in _CONSENT_PAIR.finditer(body)}
        calls.append(parsed)
    return calls


def consent_update_calls(source: str) -> int:
    """Count consent updates that would change the default after it is set."""
    return len(_CONSENT_UPDATE.findall(strip_js_comments(source)))


def script_request_urls(source: str) -> list[str]:
    """URLs a script could ask the browser to fetch."""
    return _JS_URL.findall(strip_js_comments(source))


def parse_csp(policy: str) -> dict[str, list[str]]:
    """Parse a Content-Security-Policy into lowercase directive sources."""
    parsed: dict[str, list[str]] = {}
    for part in policy.split(";"):
        tokens = part.strip().split()
        if not tokens:
            continue
        parsed[tokens[0].lower()] = [token.lower() for token in tokens[1:]]
    return parsed


def csp_problems(policy: str) -> list[str]:
    """Return ways a policy allows inline script or a non-self fetch source."""
    parsed = parse_csp(policy)
    problems: list[str] = []
    if "default-src" not in parsed:
        problems.append("missing default-src")
    for directive, sources in parsed.items():
        if directive not in _SOURCE_DIRECTIVES:
            continue
        for source in sources:
            if source not in _ALLOWED_SOURCES:
                problems.append(f"{directive} allows {source}")
    for directive in _FETCH_DIRECTIVES:
        sources = parsed.get(directive, parsed.get("default-src", []))
        if sources != ["'self'"]:
            problems.append(f"{directive} is not limited to 'self'")
    return problems


def csp_blocks_inline_script(policy: str) -> bool:
    """True when script execution cannot use an inline block or a nonce."""
    parsed = parse_csp(policy)
    sources = parsed.get("script-src", parsed.get("script-src-elem", parsed.get("default-src", [])))
    return "'unsafe-inline'" not in sources and not any(
        source.startswith("'nonce-") or source.startswith("'sha") for source in sources
    )


def _port(parts: SplitResult) -> int | None:
    port = parts.port
    if (parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80):
        return None
    return port


def same_origin(url: str, origin: str) -> bool:
    """True for same-document references and for an exactly matching origin."""
    cleaned = url.strip()
    if not cleaned or cleaned.startswith(("#", "?", "mailto:", "tel:")):
        return True
    if cleaned.startswith("/") and not cleaned.startswith("//"):
        return True
    base = urlsplit(origin)
    absolute = f"{base.scheme}:{cleaned}" if cleaned.startswith("//") else cleaned
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"} or parts.hostname is None:
        return False
    if parts.username or parts.password:
        return False
    try:
        parts_port = _port(parts)
        base_port = _port(base)
    except ValueError:
        return False
    return parts.scheme == base.scheme and (parts.hostname, parts_port) == (
        base.hostname,
        base_port,
    )


def is_external_http(url: str, origin: str) -> bool:
    """True for an absolute http(s) URL whose host is not the page host."""
    cleaned = url.strip()
    if cleaned.startswith("//"):
        cleaned = f"https:{cleaned}"
    parts = urlsplit(cleaned)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    return parts.hostname != urlsplit(origin).hostname


class _PageScan(HTMLParser):
    """Collect automatic resource URLs, anchors, inline scripts, and source items."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.asset_urls: list[str] = []
        self.anchor_urls: list[str] = []
        self.inline_scripts = 0
        self.source_items: list[tuple[str, list[str]]] = []
        self._section_depth = 0
        self._li_depth = 0
        self._li_text: list[str] = []
        self._li_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        if tag == "section":
            classes = attr.get("class", "").split()
            if self._section_depth == 0 and "sources-section" in classes:
                self._section_depth = 1
            elif self._section_depth:
                self._section_depth += 1
        if tag == "li" and self._section_depth:
            if self._li_depth == 0:
                self._li_text = []
                self._li_links = []
            self._li_depth += 1
        if tag == "a":
            href = attr.get("href", "")
            if href:
                self.anchor_urls.append(href)
                if self._li_depth:
                    self._li_links.append(href)
        asset_attr = _ASSET_ATTRIBUTES.get(tag)
        if asset_attr and attr.get(asset_attr):
            self.asset_urls.append(attr[asset_attr])
        if tag == "link" and attr.get("href"):
            self.asset_urls.append(attr["href"])
        if tag == "base" and attr.get("href"):
            self.asset_urls.append(attr["href"])
        if tag == "meta" and attr.get("http-equiv", "").lower() == "refresh":
            target = refresh_target(attr.get("content", ""))
            if target:
                self.asset_urls.append(target)
        if tag == "script":
            script_type = attr.get("type", "").split(";", 1)[0].strip().lower()
            if "src" not in attr and script_type not in _JSON_SCRIPT_TYPES:
                self.inline_scripts += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._li_depth:
            self._li_depth -= 1
            if self._li_depth == 0:
                self.source_items.append(("".join(self._li_text), list(self._li_links)))
        elif tag == "section" and self._section_depth:
            self._section_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._li_depth:
            self._li_text.append(data)


def _scan(html: str) -> _PageScan:
    parser = _PageScan()
    parser.feed(html)
    return parser


def automatic_request_urls(html: str) -> list[str]:
    """Subresources and header-like tags the browser would request from the document."""
    return _scan(html).asset_urls


def anchor_urls(html: str) -> list[str]:
    """Href values of ordinary links."""
    return _scan(html).anchor_urls


def executable_inline_scripts(html: str) -> int:
    """Inline script blocks other than JSON-LD."""
    return _scan(html).inline_scripts


def source_section_items(html: str) -> list[tuple[str, list[str]]]:
    """List items inside the authoritative-sources section."""
    return _scan(html).source_items


def source_section_problems(html: str, origin: str) -> list[str]:
    """Fail when the sources section lacks a marked external citation."""
    items = source_section_items(html)
    if not items:
        return ["sources section has no external citation"]
    problems: list[str] = []
    external_count = 0
    for text, links in items:
        for href in links:
            if not is_external_http(href, origin):
                continue
            external_count += 1
            if UNVERIFIED_CITATION_MARKER not in text:
                problems.append(f"unmarked external citation {href}")
    if external_count == 0:
        problems.append("sources section has no external citation")
    return problems


def link_header_targets(value: str) -> list[str]:
    """URLs carried by a Link header."""
    parts: list[str] = []
    current: list[str] = []
    in_angle = False
    for character in value:
        if character == "<":
            in_angle = True
        elif character == ">":
            in_angle = False
        if character == "," and not in_angle:
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    if "".join(current).strip():
        parts.append("".join(current))
    urls: list[str] = []
    for part in parts:
        match = re.search(r"<([^>]+)>", part)
        if match is not None:
            urls.append(match.group(1).strip())
    return urls


def refresh_target(value: str) -> str | None:
    """Destination of a Refresh header or meta refresh."""
    match = re.search(r"url\s*=\s*([^;]+)", value, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).strip().strip("\"'")


def _header_values(headers: Mapping[str, str], name: str) -> list[str]:
    folded = name.lower()
    return [value for key, value in headers.items() if key.lower() == folded]


def header_request_urls(headers: Mapping[str, str]) -> list[str]:
    """Resource URLs declared by response headers rather than the document body."""
    urls: list[str] = []
    for value in _header_values(headers, "link"):
        urls.extend(link_header_targets(value))
    for value in _header_values(headers, "refresh"):
        target = refresh_target(value)
        if target:
            urls.append(target)
    return urls


def publication_problems(
    html: str,
    headers: Mapping[str, str],
    origin: str,
    *,
    allow_external_anchors: bool = False,
) -> list[str]:
    """Problems in CSP, automatic resources, resource headers, and optional anchors."""
    policy = " ".join(_header_values(headers, "content-security-policy"))
    problems = [f"csp {item}" for item in csp_problems(policy)]
    for url in automatic_request_urls(html):
        if not same_origin(url, origin):
            problems.append(f"resource {url}")
    for url in header_request_urls(headers):
        if not same_origin(url, origin):
            problems.append(f"header {url}")
    if not allow_external_anchors:
        for url in anchor_urls(html):
            if not same_origin(url, origin):
                problems.append(f"anchor {url}")
    return problems


FORBIDDEN_RANKING_WORDS = ("#1", "best", "cheapest", "lowest", "recommended", "top")
FORBIDDEN_RANKING_PATTERN = re.compile(
    r"\b(best|cheapest|top|recommended|lowest)\b|#1\b",
    re.IGNORECASE,
)


class _RankingScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key == "alt" and value:
                self.texts.append(value)

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.texts.append(data)


def forbidden_ranking_problems(text: str) -> list[str]:
    """Detect words that imply an unsupported ranking or endorsement in text or HTML elements."""
    matches: set[str] = set()

    # 1. Normalized rendered text with tags stripped to catch split-markup
    # (e.g. "<b>low</b>est", "<i>be</i>st")
    stripped = re.sub(r"<[^>]+>", "", text)
    decoded_stripped = html.unescape(stripped)
    collapsed_stripped = re.sub(r"\s+", " ", decoded_stripped).strip()
    folded_stripped = collapsed_stripped.casefold()
    for m in FORBIDDEN_RANKING_PATTERN.finditer(folded_stripped):
        matches.add(m.group(0).lower())

    # 2. Normalized rendered text with tags spaced to keep separate elements distinct
    spaced = re.sub(r"<[^>]+>", " ", text)
    decoded_spaced = html.unescape(spaced)
    collapsed_spaced = re.sub(r"\s+", " ", decoded_spaced).strip()
    folded_spaced = collapsed_spaced.casefold()
    for m in FORBIDDEN_RANKING_PATTERN.finditer(folded_spaced):
        matches.add(m.group(0).lower())

    # 3. Direct unescaped text match
    decoded_raw = html.unescape(text).casefold()
    for m in FORBIDDEN_RANKING_PATTERN.finditer(decoded_raw):
        matches.add(m.group(0).lower())

    if "<" in text and ">" in text:
        scanner = _RankingScanner()
        try:
            scanner.feed(text)
            for snippet in scanner.texts:
                snip_dec = html.unescape(snippet).casefold()
                for m in FORBIDDEN_RANKING_PATTERN.finditer(snip_dec):
                    matches.add(m.group(0).lower())
        except Exception:
            pass
    return [f"forbidden ranking word '{word}'" for word in sorted(matches)]


_SQLITE_WRITE_KEYWORDS = re.compile(
    r"\b(insert\s+into|update\s+\w+|delete\s+from|drop\s+table|drop\s+view|drop\s+index|create\s+(?:(?:temp|temporary)\s+)?table|create\s+(?:(?:temp|temporary)\s+)?view|create\s+(?:(?:temp|temporary)\s+)?(?:unique\s+)?index|alter\s+table|replace\s+into|vacuum|attach\s+database|reindex|analyze)\b",
    re.IGNORECASE,
)

_PRAGMA_PATTERN = re.compile(
    r"\bpragma\s+([a-zA-Z_]+)(?:\s*(=|\()|\s*([a-zA-Z0-9_]+))?",
    re.IGNORECASE,
)

_SAFE_INTROSPECTION_PRAGMAS = frozenset(
    {
        "user_version",
        "journal_mode",
        "quick_check",
        "integrity_check",
        "application_id",
        "schema_version",
        "table_info",
        "foreign_key_list",
        "index_list",
    }
)

SAFE_SELECT_STATEMENTS = frozenset(
    {
        "SELECT payload FROM snapshot_info",
        "SELECT offer_id, payload FROM public_offers ORDER BY offer_id",
        "SELECT * FROM info",
        "SELECT 1",
    }
)


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split())


def _is_safe_select(sql: str) -> bool:
    norm = _normalize_sql(sql).lower()
    return any(norm == _normalize_sql(allowed).lower() for allowed in SAFE_SELECT_STATEMENTS)


def _check_sqlite_statement(stmt: str) -> str | None:
    if _SQLITE_WRITE_KEYWORDS.search(stmt):
        return f"sqlite write statement '{stmt.strip()[:30]}'"
    pragma_match = _PRAGMA_PATTERN.search(stmt)
    if pragma_match:
        pragma_name = pragma_match.group(1).lower()
        has_assignment = bool(pragma_match.group(2) or pragma_match.group(3))
        clean = re.sub(r"\s+", "", stmt.lower())
        if pragma_name == "query_only":
            if "query_only=on" in clean or "query_only=1" in clean or not has_assignment:
                return None
            return f"sqlite mutating pragma '{stmt.strip()[:30]}'"
        if pragma_name == "trusted_schema":
            if "trusted_schema=off" in clean or "trusted_schema=0" in clean or not has_assignment:
                return None
            return f"sqlite mutating pragma '{stmt.strip()[:30]}'"
        if pragma_name in _SAFE_INTROSPECTION_PRAGMAS:
            if not has_assignment:
                return None
            return f"sqlite mutating pragma '{stmt.strip()[:30]}'"
        return f"sqlite mutating pragma '{stmt.strip()[:30]}'"
    return None


def is_forbidden_module(name: str, *, from_module: str = "") -> bool:
    """True for database modules the web package must not reach."""
    root = name.split(".", 1)[0]
    if root == "sqlalchemy":
        return True
    if root == "sqlite3":
        # Two narrow, reviewed exceptions: the read-only snapshot reader, and the correction
        # intake store (a separate private file the web process may only add cases to; a test
        # asserts the intake package can reach nothing else of value).
        return not (
            from_module.startswith("cu_rates.snapshot") or from_module == "cu_rates.intake.store"
        )
    return name == "cu_rates.db" or name.startswith(("cu_rates.db.", "cu_rates.artifact_store"))


def is_database_reference(value: str) -> bool:
    """True for a path or name that opens the operational database."""
    normalized = value.replace("\\", "/").lower()
    filename = normalized.rsplit("/", 1)[-1]
    if filename == "published.sqlite":
        return False
    return "cu_rates.db" in normalized or filename.endswith((".db", ".sqlite", ".sqlite3"))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _package_names(sources: Mapping[str, str]) -> set[str]:
    return {name for name in sources if any(other.startswith(f"{name}.") for other in sources)}


def _relative_base(module: str, level: int, packages: set[str]) -> str:
    parts = module.split(".")
    if module not in packages and parts:
        parts = parts[:-1]
    drop = level - 1
    if drop:
        parts = parts[:-drop] if drop <= len(parts) else []
    return ".".join(parts)


def _join_module(base: str, name: str) -> str:
    if base and name:
        return f"{base}.{name}"
    return base or name


def _extract_string_expr(node: ast.AST, var_map: Mapping[str, list[str]]) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in var_map:
        return var_map[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left_strs = _extract_string_expr(node.left, var_map)
        right_strs = _extract_string_expr(node.right, var_map)
        if left_strs and right_strs:
            return [lhs + rhs for lhs in left_strs for rhs in right_strs]
    return []


def _scan_module(
    source: str,
    *,
    module: str,
    packages: set[str],
) -> tuple[set[str], list[str]]:
    tree = ast.parse(source)
    imports: set[str] = set()
    references: list[str] = []

    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent

    # Map variable names to string values assigned in this module
    var_values: dict[str, list[str]] = {}
    as_uri_vars: set[str] = set()

    for node in ast.walk(tree):
        assigned_strings: list[str] = []
        target_names: list[str] = []
        val_expr: ast.AST | None = None

        if isinstance(node, ast.Assign):
            val_expr = node.value
            for target in node.targets:
                if isinstance(target, ast.Name):
                    target_names.append(target.id)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            val_expr = node.value
            if isinstance(node.target, ast.Name):
                target_names.append(node.target.id)

        if val_expr is not None:
            has_as_uri_call = any(
                isinstance(sub, ast.Call) and _call_name(sub.func).endswith("as_uri")
                for sub in ast.walk(val_expr)
            )
            if has_as_uri_call:
                for name in target_names:
                    as_uri_vars.add(name)

            if isinstance(val_expr, ast.Constant) and isinstance(val_expr.value, str):
                assigned_strings.append(val_expr.value)
            elif isinstance(val_expr, ast.JoinedStr):
                parts = [
                    p.value
                    for p in val_expr.values
                    if isinstance(p, ast.Constant) and isinstance(p.value, str)
                ]
                assigned_strings.append(" ".join(parts))
            elif isinstance(val_expr, ast.BinOp):
                parts = []
                for sub in ast.walk(val_expr):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        parts.append(sub.value)
                if parts:
                    assigned_strings.append(" ".join(parts))

            for name in target_names:
                var_values.setdefault(name, []).extend(assigned_strings)

    sqlite3_modules: set[str] = set()
    sqlite3_connect_funcs: set[str] = {"sqlite3.connect", "connect"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
                if alias.name == "sqlite3":
                    mod_alias = alias.asname or "sqlite3"
                    sqlite3_modules.add(mod_alias)
                    sqlite3_connect_funcs.add(f"{mod_alias}.connect")
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = _join_module(_relative_base(module, node.level, packages), base)
            if base:
                imports.add(base)
            for alias in node.names:
                if alias.name != "*":
                    imports.add(_join_module(base, alias.name))
                if base == "sqlite3":
                    if alias.name == "connect":
                        fn_alias = alias.asname or "connect"
                        sqlite3_connect_funcs.add(fn_alias)
                    elif alias.name == "*":
                        sqlite3_connect_funcs.add("connect")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if isinstance(node.value, ast.Name) and node.value.id in sqlite3_modules:
                        sqlite3_modules.add(target.id)
                        sqlite3_connect_funcs.add(f"{target.id}.connect")
                    elif _call_name(node.value) in sqlite3_connect_funcs:
                        sqlite3_connect_funcs.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
        ):
            if isinstance(node.value, ast.Name) and node.value.id in sqlite3_modules:
                sqlite3_modules.add(node.target.id)
                sqlite3_connect_funcs.add(f"{node.target.id}.connect")
            elif _call_name(node.value) in sqlite3_connect_funcs:
                sqlite3_connect_funcs.add(node.target.id)

    query_only_conn_vars: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = _call_name(node.func)
            method = called.rsplit(".", 1)[-1]
            if method in {"execute", "executemany"} and node.args:
                stmt_strs: list[str] = []
                first_arg = node.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    stmt_strs.append(first_arg.value)
                elif isinstance(first_arg, ast.Name):
                    stmt_strs.extend(var_values.get(first_arg.id, []))
                for s in stmt_strs:
                    clean = re.sub(r"\s+", "", s.lower())
                    if "pragmaquery_only=on" in clean or "pragmaquery_only=1" in clean:
                        var_name = called.rsplit(".", 1)[0] if "." in called else ""
                        if var_name:
                            query_only_conn_vars.add(var_name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if is_database_reference(node.value):
                references.append(node.value)
        elif isinstance(node, ast.Call):
            called = _call_name(node.func)
            if (
                called in {"importlib.import_module", "import_module", "__import__"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                imports.add(node.args[0].value)
            if module.startswith("cu_rates.snapshot"):
                method_name = called.rsplit(".", 1)[-1]
                if method_name in {"commit", "rollback"}:
                    references.append(f"sqlite write method '{called}'")
                elif method_name == "executescript":
                    references.append(f"sqlite forbidden executescript '{called}'")
                elif method_name in {"execute", "executemany"} and node.args:
                    first_arg = node.args[0]
                    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                        problem = _check_sqlite_statement(first_arg.value)
                        if problem:
                            references.append(problem)
                        elif not (
                            _is_safe_select(first_arg.value)
                            or _PRAGMA_PATTERN.search(first_arg.value)
                        ):
                            references.append(
                                f"sqlite forbidden literal SQL '{first_arg.value.strip()[:30]}'"
                            )
                    else:
                        resolved = _extract_string_expr(first_arg, var_values)
                        if not resolved and isinstance(first_arg, ast.Name):
                            resolved = var_values.get(first_arg.id, [])
                        if not resolved:
                            name_str = _call_name(first_arg) or "expr"
                            references.append(f"sqlite forbidden non-literal SQL '{name_str}'")
                        else:
                            for stmt in resolved:
                                problem = _check_sqlite_statement(stmt)
                                if problem:
                                    references.append(problem)
                                elif not _is_safe_select(stmt):
                                    references.append(
                                        f"sqlite forbidden non-literal SQL '{stmt.strip()[:30]}'"
                                    )
                elif called in sqlite3_connect_funcs:
                    has_uri_true = any(
                        kw.arg == "uri"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                        for kw in node.keywords
                    )
                    conn_target_strings: list[str] = []
                    target_expr: ast.AST | None = None
                    if node.args:
                        target_expr = node.args[0]
                    else:
                        for kw in node.keywords:
                            if kw.arg == "database":
                                target_expr = kw.value
                                break

                    has_as_uri = False
                    if target_expr is not None:
                        if isinstance(target_expr, ast.Name) and target_expr.id in as_uri_vars:
                            has_as_uri = True
                        for sub in ast.walk(target_expr):
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                conn_target_strings.append(sub.value)
                            elif isinstance(sub, ast.Name):
                                conn_target_strings.extend(var_values.get(sub.id, []))
                                if sub.id in as_uri_vars:
                                    has_as_uri = True
                            elif isinstance(sub, ast.Call) and _call_name(sub.func).endswith(
                                "as_uri"
                            ):
                                has_as_uri = True

                    is_file_uri = has_as_uri or any(
                        urlsplit(s).scheme == "file" for s in conn_target_strings
                    )
                    has_query_mode_ro = any(_uri_is_read_only(s) for s in conn_target_strings)

                    valid_uri_ro = has_uri_true and is_file_uri and has_query_mode_ro

                    conn_vars: list[str] = []
                    curr: ast.AST | None = node
                    while curr is not None:
                        p = parent_map.get(curr)
                        if isinstance(p, ast.Assign):
                            for t in p.targets:
                                if isinstance(t, ast.Name):
                                    conn_vars.append(t.id)
                            break
                        if isinstance(p, ast.AnnAssign):
                            if isinstance(p.target, ast.Name):
                                conn_vars.append(p.target.id)
                            break
                        if isinstance(p, ast.withitem):
                            if isinstance(p.optional_vars, ast.Name):
                                conn_vars.append(p.optional_vars.id)
                            break
                        curr = p

                    if not valid_uri_ro:
                        references.append(f"sqlite writable connection '{called}'")
    return imports, references


def _uri_is_read_only(uri: str) -> bool:
    """True only when the URI's real query string says mode=ro (not a fragment or filename)."""
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(uri)
    return parse_qs(parts.query).get("mode") == ["ro"]


def database_findings(sources: Mapping[str, str], roots: Sequence[str]) -> list[str]:
    """Import and open findings for roots and the cu_rates modules they reach."""
    packages = _package_names(sources)
    findings: list[str] = []
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        source = sources.get(module)
        if source is None:
            if is_forbidden_module(module):
                findings.append(f"import {module}")
            continue
        imports, references = _scan_module(source, module=module, packages=packages)
        for reference in references:
            if (
                reference.startswith("sqlite write")
                or reference.startswith("sqlite forbidden")
                or reference.startswith("sqlite mutating")
                or reference.startswith("sqlite writable")
            ):
                findings.append(f"{reference} in {module}")
            else:
                findings.append(f"open {reference} in {module}")
        for imported in sorted(imports):
            if is_forbidden_module(imported, from_module=module):
                findings.append(f"import {imported} from {module}")
            elif imported in sources and imported not in seen:
                queue.append(imported)
    return findings


def load_cu_rates_sources(src_root: Path) -> dict[str, str]:
    """Read the cu_rates package as module-name to source text."""
    package = src_root / "cu_rates"
    sources: dict[str, str] = {}
    for path in package.rglob("*.py"):
        parts = list(path.relative_to(src_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        sources[".".join(parts)] = path.read_text(encoding="utf-8")
    return sources
