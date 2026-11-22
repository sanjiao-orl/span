"""Zero-dependency standard-library EPUB 2/3 reader (reader: epub-1)."""

from dataclasses import dataclass
from html.parser import HTMLParser
import io
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile


EPUB_READER_VERSION = "epub-1"
MAX_EPUB_ENTRY_BYTES = 32 * 1024 * 1024
MAX_EPUB_TOTAL_BYTES = 128 * 1024 * 1024


class IncompleteSpineError(ValueError):
    """A linear spine reference cannot be resolved without losing book content."""


class Utf16DecodingError(ValueError):
    """An explicitly BOM-marked entry failed the new UTF-16 decoding rule."""

FONT_ALGORITHMS = frozenset((
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#rc",
))
FONT_EXTENSIONS = (".otf", ".ttf", ".woff", ".woff2")
CONTENT_EXTENSIONS = (".xhtml", ".html", ".htm", ".xml", ".opf", ".ncx")

BLOCK_TAGS = frozenset((
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "blockquote", "tr", "br", "hr",
    "section", "article", "header", "footer", "pre", "aside",
))
HEADING_TAGS = frozenset(("h1", "h2", "h3", "h4", "h5", "h6"))
PRIMARY_HEADING_TAGS = frozenset(("h1", "h2", "h3"))
IGNORED_TAGS = frozenset(("head", "script", "style", "svg", "noscript"))

_BARE_AMP_RE = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#x[0-9a-fA-F]+);)")
_XML_ENCODING_RE = re.compile(br'(?:encoding|charset)\s*=\s*["\']?([A-Za-z0-9._-]+)', re.I)

_CJK_CHAR = (
    r"\u2014\u2018\u2019\u201c\u201d\u2026\u00b7"
    r"\u2e80-\u33ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef"
    r"\U00020000-\U0002ebe0"
)
_AROUND_NEWLINE_RE = re.compile(r"[ \t\f\v]*[\r\n]+[ \t\f\v]*")
_CJK_NEWLINE_RE = re.compile(rf"([{_CJK_CHAR}])\n(?=[{_CJK_CHAR}])")
_ASCII_SPACE_RE = re.compile(r"[ \t\f\v]+")


@dataclass(frozen=True)
class DerivedLine:
    text: str
    heading_tag: str | None = None


def _local_name(tag):
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag


def _normalize_ws(text):
    return " ".join(text.split())


def normalize_inline_text(raw_text):
    """Normalize intra-block newlines per CSS Text 3 segment break rules."""
    text = _AROUND_NEWLINE_RE.sub("\n", raw_text)
    text = _CJK_NEWLINE_RE.sub(r"\1", text)
    text = text.replace("\n", " ")
    text = _ASCII_SPACE_RE.sub(" ", text)
    return text.strip()


def _decode_xml_bytes(raw, *, legacy=False):
    if not legacy and raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise Utf16DecodingError("EPUB UTF-16 BOM 文本编码损坏") from exc
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        match = _XML_ENCODING_RE.search(raw[:1024])
        if match is not None:
            enc = match.group(1).decode("ascii", errors="ignore")
            try:
                return raw.decode(enc)
            except (LookupError, UnicodeDecodeError):
                pass
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def _parse_xml_root(raw, label, *, decode=_decode_xml_bytes):
    text = decode(raw)
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        sanitized = _BARE_AMP_RE.sub("&amp;", text)
        try:
            return ET.fromstring(sanitized)
        except ET.ParseError as exc:
            raise ValueError(f"无效的 EPUB 文件：{label} XML 格式损坏") from exc


def _resolve_epub_href(base_dir, raw_href):
    if not raw_href:
        return "", None
    cleaned = raw_href.strip().replace("\\", "/")
    if "#" in cleaned:
        path_part, fragment = cleaned.split("#", 1)
        fragment = urllib.parse.unquote(fragment).strip() or None
    else:
        path_part, fragment = cleaned, None
    path_part = urllib.parse.unquote(path_part).strip()
    if not path_part:
        return "", fragment
    joined = posixpath.normpath(posixpath.join(base_dir, path_part)) if base_dir else posixpath.normpath(path_part)
    while joined.startswith("./"):
        joined = joined[2:]
    if joined == ".":
        joined = ""
    return joined, fragment


def _build_zip_lookup(zf):
    exact = set()
    casefolded = {}
    for name in zf.namelist():
        exact.add(name)
        norm = posixpath.normpath(name.replace("\\", "/"))
        casefolded.setdefault(norm.casefold(), name)
    return exact, casefolded


def _find_zip_entry(name, exact, casefolded):
    if not name:
        return None
    if name in exact:
        return name
    norm = posixpath.normpath(name.replace("\\", "/"))
    if norm in exact:
        return norm
    return casefolded.get(norm.casefold())


def _read_zip_entry(zf, entry_name, total_read):
    try:
        info = zf.getinfo(entry_name)
    except KeyError as exc:
        raise ValueError(f"EPUB 缺少内部文件：{entry_name}") from exc
    if info.file_size > MAX_EPUB_ENTRY_BYTES:
        raise ValueError(f"EPUB 内部文件 {entry_name} 解压体积超过单文件安全上限（32 MB）")
    if total_read + info.file_size > MAX_EPUB_TOTAL_BYTES:
        raise ValueError("EPUB 文本解压总体积超过安全上限（128 MB）")
    with zf.open(info, "r") as fp:
        data = fp.read(MAX_EPUB_ENTRY_BYTES + 1)
    if len(data) > MAX_EPUB_ENTRY_BYTES:
        raise ValueError(f"EPUB 内部文件 {entry_name} 解压体积超过单文件安全上限（32 MB）")
    total_read += len(data)
    if total_read > MAX_EPUB_TOTAL_BYTES:
        raise ValueError("EPUB 文本解压总体积超过安全上限（128 MB）")
    return data, total_read


def _check_encryption_xml(zf, exact, casefolded, total_read, *, decode=_decode_xml_bytes):
    enc_entry = _find_zip_entry("META-INF/encryption.xml", exact, casefolded)
    if enc_entry is None:
        return total_read
    raw, total_read = _read_zip_entry(zf, enc_entry, total_read)
    try:
        root = _parse_xml_root(raw, "META-INF/encryption.xml", decode=decode)
    except ValueError as exc:
        raise ValueError("该 EPUB 含有 DRM 加密，无法直接读取") from exc
    if _local_name(root.tag).lower() != "encryption":
        raise ValueError("该 EPUB 含有 DRM 加密，无法直接读取")

    encrypted_data_nodes = [elem for elem in root.iter() if _local_name(elem.tag) == "EncryptedData"]
    if not encrypted_data_nodes and list(root):
        raise ValueError("该 EPUB 含有 DRM 加密，无法直接读取")

    for node in encrypted_data_nodes:
        alg = ""
        uri = ""
        for child in node.iter():
            cname = _local_name(child.tag)
            if cname == "EncryptionMethod":
                alg = (child.attrib.get("Algorithm") or child.attrib.get("algorithm") or "").strip().lower()
            elif cname == "CipherReference":
                uri = (child.attrib.get("URI") or child.attrib.get("uri") or "").strip()
        uri_clean = urllib.parse.unquote(uri).split("#", 1)[0].strip().lower()
        is_font_uri = uri_clean.endswith(FONT_EXTENSIONS)
        is_content_uri = uri_clean.endswith(CONTENT_EXTENSIONS)
        is_font_alg = alg in FONT_ALGORITHMS
        if is_content_uri or (alg and not is_font_alg) or (not is_font_uri and not is_font_alg):
            raise ValueError("该 EPUB 含有 DRM 加密，无法直接读取")
    return total_read


class _NavHTMLParser(HTMLParser):
    """Extract TOC links from EPUB 3 nav.xhtml using html.parser."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignore_depth = 0
        self._in_rt_rp = False
        self._nav_stack = []
        self.nav_blocks = []
        self._current_a_href = None
        self._current_a_buf = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "body":
            self._ignore_depth = 0
        if t in IGNORED_TAGS:
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return
        if t in ("rt", "rp"):
            self._in_rt_rp = True
            return
        if t in ("rb", "rtc"):
            self._in_rt_rp = False
        if t == "nav":
            is_toc = False
            for k, v in attrs:
                if not v:
                    continue
                kl = k.lower()
                tokens = v.lower().split()
                if (kl.endswith("type") or kl == "role") and ("toc" in tokens or "doc-toc" in tokens):
                    is_toc = True
                    break
            block = {"is_toc": is_toc, "links": []}
            self._nav_stack.append(block)
            self.nav_blocks.append(block)
        elif t == "a" and self._nav_stack:
            attr_dict = dict(attrs)
            href = attr_dict.get("href")
            if href is not None:
                self._current_a_href = href
                self._current_a_buf = []

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in IGNORED_TAGS:
            if self._ignore_depth > 0:
                self._ignore_depth -= 1
            return
        if self._ignore_depth > 0:
            return
        if t in ("rt", "rp", "ruby"):
            self._in_rt_rp = False
            return
        if t == "a" and self._current_a_href is not None:
            label = normalize_inline_text("".join(self._current_a_buf))
            if self._nav_stack:
                self._nav_stack[-1]["links"].append((label, self._current_a_href))
            self._current_a_href = None
            self._current_a_buf = []
        elif t == "nav" and self._nav_stack:
            self._nav_stack.pop()

    def handle_data(self, data):
        if self._ignore_depth > 0 or self._in_rt_rp:
            return
        if self._current_a_href is not None:
            self._current_a_buf.append(data)


class _NcxHTMLParser(HTMLParser):
    """Fallback parser for malformed toc.ncx files."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._stack = []
        self._in_text = False
        self._text_buf = []
        self.entries = []

    def handle_starttag(self, tag, attrs):
        t = _local_name(tag).lower()
        if t == "navpoint":
            entry = {"label": "", "src": ""}
            self.entries.append(entry)
            self._stack.append(entry)
        elif t == "text" and self._stack:
            self._in_text = True
            self._text_buf = []
        elif t == "content" and self._stack:
            attr_dict = {k.lower(): v for k, v in attrs if v is not None}
            if not self._stack[-1]["src"]:
                self._stack[-1]["src"] = attr_dict.get("src", "")

    def handle_endtag(self, tag):
        t = _local_name(tag).lower()
        if t == "text" and self._in_text:
            self._in_text = False
            if self._stack and not self._stack[-1]["label"]:
                self._stack[-1]["label"] = normalize_inline_text("".join(self._text_buf))
            self._text_buf = []
        elif t == "navpoint" and self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if self._in_text:
            self._text_buf.append(data)


def _parse_nav_toc(raw, nav_href, *, decode=_decode_xml_bytes):
    nav_dir = posixpath.dirname(nav_href)
    parser = _NavHTMLParser()
    parser.feed(decode(raw))
    parser.close()
    selected = next((b for b in parser.nav_blocks if b["is_toc"] and b["links"]), None)
    if selected is None:
        selected = next((b for b in parser.nav_blocks if b["links"]), None)
    if selected is None:
        return []
    entries = []
    for label, raw_href in selected["links"]:
        if not label or not raw_href or not raw_href.strip():
            continue
        href, fragment = _resolve_epub_href(nav_dir, raw_href)
        if not href or href == nav_href:
            continue
        entries.append({"title": label, "href": href, "fragment": fragment})
    return entries


def _parse_ncx_toc(raw, ncx_href, *, decode=_decode_xml_bytes):
    ncx_dir = posixpath.dirname(ncx_href)
    text = decode(raw)
    sanitized = _BARE_AMP_RE.sub("&amp;", text)
    entries = []
    try:
        root = ET.fromstring(sanitized)
        nav_map = next((el for el in root.iter() if _local_name(el.tag).lower() == "navmap"), root)

        def walk(node):
            for child in node:
                if _local_name(child.tag).lower() != "navpoint":
                    walk(child)
                    continue
                label = ""
                src = ""
                for sub in child:
                    sname = _local_name(sub.tag).lower()
                    if sname == "navlabel" and not label:
                        for tnode in sub.iter():
                            if _local_name(tnode.tag).lower() == "text":
                                raw_label = "".join(tnode.itertext())
                                label = normalize_inline_text(raw_label)
                                if label:
                                    break
                    elif sname == "content" and not src:
                        src = sub.attrib.get("src", "")
                if label and src:
                    href, fragment = _resolve_epub_href(ncx_dir, src)
                    if href:
                        entries.append({"title": label, "href": href, "fragment": fragment})
                walk(child)

        walk(nav_map)
        return entries
    except ET.ParseError:
        fallback = _NcxHTMLParser()
        fallback.feed(text)
        fallback.close()
        for item in fallback.entries:
            if item["label"] and item["src"]:
                href, fragment = _resolve_epub_href(ncx_dir, item["src"])
                if href:
                    entries.append({"title": item["label"], "href": href, "fragment": fragment})
        return entries


class _EpubContentHTMLParser(HTMLParser):
    """Extract derived lines, heading tags, anchor positions, and image count from XHTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignore_depth = 0
        self._in_rt_rp = False
        self._current_heading = None
        self._pre_depth = 0
        self._buf = []
        self.lines = []
        self.anchors = {}
        self.img_count = 0

    def _flush_line(self):
        if not self._buf:
            return
        raw = "".join(self._buf)
        self._buf = []
        if self._pre_depth > 0:
            for raw_line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                cleaned = raw_line.strip()
                if cleaned:
                    self.lines.append(DerivedLine(cleaned, self._current_heading))
            return
        cleaned = normalize_inline_text(raw)
        if cleaned:
            self.lines.append(DerivedLine(cleaned, self._current_heading))

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "body":
            self._ignore_depth = 0
        if t in IGNORED_TAGS:
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return
        if t in ("rt", "rp"):
            self._in_rt_rp = True
            return
        if t in ("rb", "rtc"):
            self._in_rt_rp = False
        if t == "img":
            self.img_count += 1
        if t in BLOCK_TAGS:
            if t == "br" and self._current_heading is not None:
                self._buf.append(" ")
            else:
                self._flush_line()
        if t in HEADING_TAGS:
            self._current_heading = t
        elif t == "pre":
            self._pre_depth += 1

        for attr_name, attr_val in attrs:
            if attr_name in ("id", "name") and attr_val:
                key = attr_val.strip()
                if key and key not in self.anchors:
                    self.anchors[key] = len(self.lines)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in IGNORED_TAGS:
            if self._ignore_depth > 0:
                self._ignore_depth -= 1
            return
        if self._ignore_depth > 0:
            return
        if t in ("rt", "rp", "ruby"):
            self._in_rt_rp = False
            return
        if t in BLOCK_TAGS and not (t == "br" and self._current_heading is not None):
            self._flush_line()
        if t in HEADING_TAGS and self._current_heading == t:
            self._current_heading = None
        elif t == "pre" and self._pre_depth > 0:
            self._pre_depth -= 1

    def handle_data(self, data):
        if self._ignore_depth > 0 or self._in_rt_rp:
            return
        self._buf.append(data)

    def finish(self):
        self._flush_line()


def extract_xhtml_content(raw, *, decode=_decode_xml_bytes):
    parser = _EpubContentHTMLParser()
    parser.feed(decode(raw))
    parser.close()
    parser.finish()
    return parser.lines, parser.anchors, parser.img_count


def _append_spine_range(spine_map, href, start_line, end_line):
    if end_line < start_line:
        return
    if spine_map and spine_map[-1]["href"] == href and spine_map[-1]["end_line"] + 1 == start_line:
        spine_map[-1]["end_line"] = end_line
    else:
        spine_map.append({"href": href, "start_line": start_line, "end_line": end_line})


def _classify_title_kind(title, *, peripheral_re, unclassified_re, default_kind="body"):
    label = title.strip()
    if peripheral_re.fullmatch(label):
        return "peripheral"
    if unclassified_re.fullmatch(label):
        return "unclassified"
    return default_kind


def _deduplicate_leading_heading(slice_lines, title):
    if not slice_lines:
        return []
    first = slice_lines[0]
    if first.heading_tag in HEADING_TAGS and _normalize_ws(first.text) == _normalize_ws(title):
        return [line.text for line in slice_lines[1:]]
    return [line.text for line in slice_lines]


def _spine_fallback_title(item_lines, href):
    for line in item_lines:
        if line.heading_tag in PRIMARY_HEADING_TAGS and line.text.strip():
            return line.text.strip()
    for line in item_lines:
        if line.heading_tag in HEADING_TAGS and line.text.strip():
            return line.text.strip()
    stem = posixpath.splitext(posixpath.basename(href))[0].strip()
    return stem or "未命名章节"


def parse_epub_bytes(
    raw,
    *,
    source_name="",
    chapter_pattern=None,
    paragraphs="lines",
    single_chapter=False,
    allow_incomplete_spine=False,
    legacy_xml_decoding=False,
    peripheral_re,
    unclassified_re,
    chapter_sections_fn,
):
    xml_parsing = {}

    def decode(data):
        if not legacy_xml_decoding and data.startswith((b"\xff\xfe", b"\xfe\xff")):
            # Only affected books opt into new derivation rules. Stored snapshots
            # without this marker can still replay the original epub-1 decoder.
            xml_parsing["xml_decoding"] = "utf16-bom-1"
        return _decode_xml_bytes(data, legacy=legacy_xml_decoding)

    try:
        zf_ctx = zipfile.ZipFile(io.BytesIO(raw), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("无效的 EPUB 文件（不是合法的 ZIP 容器）") from exc

    with zf_ctx as zf:
        exact, casefolded = _build_zip_lookup(zf)
        total_read = 0
        total_read = _check_encryption_xml(zf, exact, casefolded, total_read, decode=decode)

        container_entry = _find_zip_entry("META-INF/container.xml", exact, casefolded)
        if container_entry is None:
            raise ValueError("无效的 EPUB 文件：缺少 META-INF/container.xml")
        container_raw, total_read = _read_zip_entry(zf, container_entry, total_read)
        container_root = _parse_xml_root(container_raw, "META-INF/container.xml", decode=decode)

        opf_raw_path = ""
        for elem in container_root.iter():
            if _local_name(elem.tag).lower() == "rootfile":
                opf_raw_path = elem.attrib.get("full-path", "").strip()
                if opf_raw_path:
                    break
        opf_path, _ = _resolve_epub_href("", opf_raw_path)
        opf_entry = _find_zip_entry(opf_path, exact, casefolded)
        if not opf_path or opf_entry is None:
            raise ValueError("无效的 EPUB 文件：缺少 OPF 包清单文件")

        opf_dir = posixpath.dirname(opf_entry)
        opf_raw, total_read = _read_zip_entry(zf, opf_entry, total_read)
        opf_root = _parse_xml_root(opf_raw, opf_entry, decode=decode)

        dc_title = ""
        manifest = {}
        spine_refs = []
        spine_toc_id = ""
        nav_href = ""
        ncx_href = ""

        for child in opf_root:
            cname = _local_name(child.tag).lower()
            if cname == "metadata":
                for meta_el in child.iter():
                    if _local_name(meta_el.tag).lower() == "title" and not dc_title:
                        text = normalize_inline_text("".join(meta_el.itertext()))
                        if text:
                            dc_title = text
            elif cname == "manifest":
                for item in child:
                    if _local_name(item.tag).lower() != "item":
                        continue
                    item_id = item.attrib.get("id", "").strip()
                    raw_href = item.attrib.get("href", "")
                    mtype = item.attrib.get("media-type", "").strip().lower()
                    props = set(item.attrib.get("properties", "").split())
                    full_href, _ = _resolve_epub_href(opf_dir, raw_href)
                    resolved_entry = _find_zip_entry(full_href, exact, casefolded) or full_href
                    manifest[item_id] = {
                        "id": item_id,
                        "href": resolved_entry,
                        "media_type": mtype,
                        "properties": props,
                    }
                    if "nav" in props and not nav_href:
                        nav_href = resolved_entry
                    if mtype == "application/x-dtbncx+xml" and not ncx_href:
                        ncx_href = resolved_entry
            elif cname == "spine":
                spine_toc_id = child.attrib.get("toc", "").strip()
                for itemref in child:
                    if _local_name(itemref.tag).lower() != "itemref":
                        continue
                    spine_refs.append({
                        "idref": itemref.attrib.get("idref", "").strip(),
                        "linear": itemref.attrib.get("linear", "yes").strip().lower(),
                    })

        if not ncx_href and spine_toc_id and spine_toc_id in manifest:
            ncx_href = manifest[spine_toc_id]["href"]

        toc_entries = []
        nav_entry = _find_zip_entry(nav_href, exact, casefolded) if nav_href else None
        if nav_entry is not None:
            nav_raw, total_read = _read_zip_entry(zf, nav_entry, total_read)
            toc_entries = _parse_nav_toc(nav_raw, nav_entry, decode=decode)
        if not toc_entries:
            ncx_entry = _find_zip_entry(ncx_href, exact, casefolded) if ncx_href else None
            if ncx_entry is not None:
                ncx_raw, total_read = _read_zip_entry(zf, ncx_entry, total_read)
                toc_entries = _parse_ncx_toc(ncx_raw, ncx_entry, decode=decode)

        for entry in toc_entries:
            entry["href"] = _find_zip_entry(entry["href"], exact, casefolded) or entry["href"]

        skipped_spine_items = []
        image_count = 0
        non_empty_spine = []
        seen_hrefs = set()

        for ref in spine_refs:
            mitem = manifest.get(ref["idref"])
            if mitem is None:
                if ref["linear"] != "no" and not allow_incomplete_spine:
                    raise IncompleteSpineError(f"EPUB Spine 引用的条目不在 Manifest 中：{ref['idref'] or '（空 idref）'}")
                continue
            href = mitem["href"]
            if not href:
                if ref["linear"] != "no" and not allow_incomplete_spine:
                    raise IncompleteSpineError(f"EPUB Spine 条目缺少内部文件路径：{ref['idref']}")
                continue
            entry_in_zip = _find_zip_entry(href, exact, casefolded)
            if entry_in_zip is None and ref["linear"] != "no" and not allow_incomplete_spine:
                raise IncompleteSpineError(f"EPUB 缺少 Spine 内部文件：{href}")
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            if href == nav_entry:
                skipped_spine_items.append(href)
                continue

            if entry_in_zip is None:
                skipped_spine_items.append(href)
                continue

            item_raw, total_read = _read_zip_entry(zf, entry_in_zip, total_read)
            item_lines, item_anchors, item_imgs = extract_xhtml_content(item_raw, decode=decode)
            image_count += item_imgs

            if ref["linear"] == "no" or not item_lines:
                skipped_spine_items.append(entry_in_zip)
                continue

            non_empty_spine.append({
                "href": entry_in_zip,
                "lines": item_lines,
                "anchors": item_anchors,
            })

    if single_chapter:
        derived_lines = []
        spine_map = []
        first_heading = ""
        for item in non_empty_spine:
            start_1based = len(derived_lines) + 1
            for dline in item["lines"]:
                if not first_heading and dline.heading_tag in HEADING_TAGS:
                    first_heading = dline.text
                derived_lines.append(dline.text)
            _append_spine_range(spine_map, item["href"], start_1based, len(derived_lines))
        title = first_heading or dc_title or posixpath.splitext(posixpath.basename(source_name))[0] or "单章正文"
        parsing = {
            "format": "epub",
            "reader": EPUB_READER_VERSION,
            "chapter_source": "single_chapter",
            "single_chapter": True,
            "paragraphs": paragraphs,
            "newlines": "LF",
            **xml_parsing,
        }
        sections = [(-1, title, "body")] if derived_lines else []
        return {
            "lines": derived_lines,
            "sections": sections,
            "candidates": [],
            "parsing": parsing,
            "chapter_source": "single_chapter",
            "skipped_spine_items": skipped_spine_items,
            "image_count": image_count,
            "spine_map": spine_map,
        }

    if chapter_pattern is not None:
        derived_lines = []
        spine_map = []
        for item in non_empty_spine:
            start_1based = len(derived_lines) + 1
            derived_lines.extend(dline.text for dline in item["lines"])
            _append_spine_range(spine_map, item["href"], start_1based, len(derived_lines))
        try:
            sections, candidates = chapter_sections_fn(derived_lines, chapter_pattern)
        except re.error as exc:
            raise ValueError(f"无效的分章正则：{exc}") from exc
        parsing = {
            "format": "epub",
            "reader": EPUB_READER_VERSION,
            "chapter_source": "pattern",
            "chapter_pattern": chapter_pattern,
            "paragraphs": paragraphs,
            "newlines": "LF",
            **xml_parsing,
        }
        return {
            "lines": derived_lines,
            "sections": sections,
            "candidates": candidates,
            "parsing": parsing,
            "chapter_source": "pattern",
            "skipped_spine_items": skipped_spine_items,
            "image_count": image_count,
            "spine_map": spine_map,
        }

    toc_by_href = {}
    for entry in toc_entries:
        toc_by_href.setdefault(entry["href"], []).append(entry)

    has_toc_match = any(item["href"] in toc_by_href for item in non_empty_spine)
    chapter_source = "toc" if has_toc_match else "spine"

    derived_lines = []
    sections = []
    spine_map = []

    def emit_chapter(href, title, kind, slice_lines):
        start_1based = len(derived_lines) + 1
        heading_index = len(derived_lines)
        derived_lines.append(title)
        body_texts = _deduplicate_leading_heading(slice_lines, title)
        derived_lines.extend(body_texts)
        sections.append((heading_index, title, kind))
        _append_spine_range(spine_map, href, start_1based, len(derived_lines))

    if chapter_source == "toc":
        for item in non_empty_spine:
            href = item["href"]
            item_lines = item["lines"]
            anchors = item["anchors"]
            anchors_ci = {k.casefold(): v for k, v in anchors.items()}
            item_toc = toc_by_href.get(href)

            if not item_toc:
                title = _spine_fallback_title(item_lines, href)
                kind = _classify_title_kind(
                    title,
                    peripheral_re=peripheral_re,
                    unclassified_re=unclassified_re,
                    default_kind="unclassified",
                )
                emit_chapter(href, title, kind, item_lines)
                continue

            if len(item_toc) == 1:
                title = item_toc[0]["title"]
                kind = _classify_title_kind(
                    title,
                    peripheral_re=peripheral_re,
                    unclassified_re=unclassified_re,
                    default_kind="body",
                )
                emit_chapter(href, title, kind, item_lines)
                continue

            resolved = []
            for entry in item_toc:
                frag = entry["fragment"]
                if not frag:
                    idx = 0
                elif frag in anchors:
                    idx = anchors[frag]
                elif frag.casefold() in anchors_ci:
                    idx = anchors_ci[frag.casefold()]
                else:
                    continue
                idx = max(0, min(idx, len(item_lines)))
                if resolved and idx < resolved[-1][0]:
                    continue
                resolved.append((idx, entry["title"]))

            if not resolved:
                title = item_toc[0]["title"]
                kind = _classify_title_kind(
                    title,
                    peripheral_re=peripheral_re,
                    unclassified_re=unclassified_re,
                    default_kind="body",
                )
                emit_chapter(href, title, kind, item_lines)
                continue

            first_idx = resolved[0][0]
            if first_idx > 0:
                if not sections:
                    start_1based = len(derived_lines) + 1
                    derived_lines.extend(dline.text for dline in item_lines[:first_idx])
                    _append_spine_range(spine_map, href, start_1based, len(derived_lines))
                else:
                    resolved[0] = (0, resolved[0][1])

            stops = [idx for idx, _ in resolved[1:]] + [len(item_lines)]
            for (start_idx, title), stop_idx in zip(resolved, stops):
                slice_lines = item_lines[start_idx:stop_idx]
                kind = _classify_title_kind(
                    title,
                    peripheral_re=peripheral_re,
                    unclassified_re=unclassified_re,
                    default_kind="body",
                )
                emit_chapter(href, title, kind, slice_lines)
    else:
        for item in non_empty_spine:
            href = item["href"]
            item_lines = item["lines"]
            title = _spine_fallback_title(item_lines, href)
            kind = _classify_title_kind(
                title,
                peripheral_re=peripheral_re,
                unclassified_re=unclassified_re,
                default_kind="body",
            )
            emit_chapter(href, title, kind, item_lines)

    parsing = {
        "format": "epub",
        "reader": EPUB_READER_VERSION,
        "chapter_source": chapter_source,
        "paragraphs": paragraphs,
        "newlines": "LF",
        **xml_parsing,
    }
    return {
        "lines": derived_lines,
        "sections": sections,
        "candidates": [],
        "parsing": parsing,
        "chapter_source": chapter_source,
        "skipped_spine_items": skipped_spine_items,
        "image_count": image_count,
        "spine_map": spine_map,
    }
