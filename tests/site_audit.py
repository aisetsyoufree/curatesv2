"""Detectors for website publication guarantees.

Legacy helpers reproduce the previous checks so tests can show an injected defect
slipping through, then the corrected detector catching it.
"""

import ast
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


FORBIDDEN_RANKING_WORDS = ("best", "cheapest", "top", "recommended")
FORBIDDEN_RANKING_PATTERN = re.compile(
    r"\b(best|cheapest|top|recommended)\b",
    re.IGNORECASE,
)


def forbidden_ranking_problems(text: str) -> list[str]:
    """Detect words that imply an unsupported ranking or endorsement."""
    matches = FORBIDDEN_RANKING_PATTERN.findall(text)
    return [f"forbidden ranking word '{word.lower()}'" for word in sorted(set(matches))]


def is_forbidden_module(name: str, *, from_module: str = "") -> bool:
    """True for database modules the web package must not reach."""
    root = name.split(".", 1)[0]
    if root == "sqlalchemy":
        return True
    if root == "sqlite3":
        return not from_module.startswith("cu_rates.snapshot")
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


def _scan_module(
    source: str,
    *,
    module: str,
    packages: set[str],
) -> tuple[set[str], list[str]]:
    tree = ast.parse(source)
    imports: set[str] = set()
    references: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = _join_module(_relative_base(module, node.level, packages), base)
            if base:
                imports.add(base)
            for alias in node.names:
                if alias.name != "*":
                    imports.add(_join_module(base, alias.name))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
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
    return imports, references


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
        findings.extend(f"open {reference} in {module}" for reference in references)
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
