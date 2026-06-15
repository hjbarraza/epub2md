#!/usr/bin/env python3
import re, shutil, stat, subprocess, sys, tempfile, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

MAX_ARCHIVE_FILES = 10000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
MAX_SLUG_LENGTH = 60

LUA = """
function Div(el) return el.content end
function Span(el) return el.content end
function Para(el)
  if el.content and #el.content==1 and el.content[1].t=='Str' and el.content[1].text=='\\\\' then return {} end
  return el
end
function Plain(el)
  if el.content and #el.content==1 and el.content[1].t=='Str' and el.content[1].text=='\\\\' then return {} end
  return el
end
function Image(el) el.classes={} el.attributes={} return el end
"""


class EpubError(Exception):
    pass


def _local_name(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _href_path(href, allow_parent=False):
    href = href.replace("\\", "/")
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise EpubError(f"unsafe external path: {href}")
    path = unquote(parsed.path)
    if not path:
        raise EpubError("empty path")
    rel = PurePosixPath(path)
    if rel.is_absolute() or (not allow_parent and ".." in rel.parts):
        raise EpubError(f"unsafe path traversal: {href}")
    return Path(*rel.parts)


def _inside(root, path):
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _safe_child(root, base, href, allow_parent=False):
    path = (base / _href_path(href, allow_parent=allow_parent)).resolve()
    if not _inside(root, path):
        raise EpubError(f"path escapes EPUB: {href}")
    return path


def _safe_media_name(name):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return safe or "image"


def _safe_output_slug(title):
    safe = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return safe[:MAX_SLUG_LENGTH].rstrip("-") or "untitled"


def _copy_media(source, media, copied, used_names):
    key = str(source.resolve())
    if key in copied:
        return copied[key]

    safe = _safe_media_name(source.name)
    stem = Path(safe).stem or "image"
    suffix = Path(safe).suffix
    candidate = safe
    i = 2
    while candidate in used_names:
        candidate = f"{stem}-{i}{suffix}"
        i += 1

    used_names.add(candidate)
    target = media / candidate
    shutil.copyfile(source, target)
    link = f"images/{candidate}"
    copied[key] = link
    return link


def _prepare_html_for_pandoc(root, html_path, media, work_dir, copied, used_names):
    try:
        tree = ET.parse(html_path)
    except ET.ParseError:
        return html_path

    changed = False
    for el in tree.getroot().iter():
        if _local_name(el.tag) != "img":
            continue

        src = el.attrib.get("src")
        if not src:
            continue
        try:
            image_path = _safe_child(root, html_path.parent, src, allow_parent=True)
        except EpubError:
            el.attrib.pop("src", None)
            changed = True
            continue
        if not image_path.is_file():
            el.attrib.pop("src", None)
            changed = True
            continue

        el.set("src", _copy_media(image_path, media, copied, used_names))
        changed = True

    if not changed:
        return html_path

    prepared = work_dir / f"{len(list(work_dir.iterdir())):05d}-{html_path.name}"
    tree.write(prepared, encoding="utf-8", xml_declaration=True)
    return prepared


def _zip_member_path(root, name):
    name = name.replace("\\", "/")
    rel = PurePosixPath(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise EpubError(f"unsafe archive path: {name}")
    return root.joinpath(*rel.parts)


def _is_symlink(info):
    mode = info.external_attr >> 16
    return info.create_system == 3 and stat.S_ISLNK(mode)


def _extract_epub(epub, dest):
    try:
        archive = zipfile.ZipFile(epub)
    except zipfile.BadZipFile as exc:
        raise EpubError("invalid EPUB archive") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise EpubError("EPUB archive has too many files")

        total_size = 0
        for info in infos:
            _zip_member_path(dest, info.filename)
            if _is_symlink(info):
                raise EpubError(f"archive symlinks are not allowed: {info.filename}")
            if info.file_size > MAX_ARCHIVE_FILE_BYTES:
                raise EpubError(f"archive file too large: {info.filename}")
            total_size += info.file_size
            if total_size > MAX_ARCHIVE_BYTES:
                raise EpubError("EPUB archive is too large")
            if (
                info.compress_size
                and info.file_size > 1024 * 1024
                and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise EpubError(f"suspicious compression ratio: {info.filename}")

        for info in infos:
            target = _zip_member_path(dest, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _find_opf(root):
    container = root / "META-INF" / "container.xml"
    if not container.exists():
        return None
    try:
        tree = ET.parse(container)
    except ET.ParseError:
        return None
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = tree.find(".//c:rootfile", ns)
    if rootfile is None:
        return None
    full_path = rootfile.attrib.get("full-path")
    if not full_path:
        return None
    try:
        opf = _safe_child(root, root, full_path)
    except EpubError:
        return None
    return opf if opf.exists() else None


def _parse_opf(root):
    opf = _find_opf(root)
    if opf is None:
        return None, {}, None
    try:
        tree = ET.parse(opf)
    except ET.ParseError:
        return opf, {}, None
    pkg = tree.getroot()
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    manifest_el = pkg.find("opf:manifest", ns)
    manifest = {}
    if manifest_el is not None:
        for item in manifest_el:
            item_id = item.attrib.get("id")
            if item_id:
                manifest[item_id] = item
    return opf, manifest, pkg.find("opf:spine", ns)


def _split_href(href):
    parsed = urlsplit(href.replace("\\", "/"))
    _href_path(href)
    fragment = unquote(parsed.fragment) if parsed.fragment else None
    return unquote(parsed.path), fragment


def _parse_ncx(ncx_path):
    try:
        tree = ET.parse(ncx_path)
    except ET.ParseError:
        return ncx_path.parent, []
    ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
    items = []
    for nav in tree.findall(".//n:navPoint", ns):
        te = nav.find(".//n:text", ns)
        ce = nav.find(".//n:content", ns)
        if te is None or ce is None:
            continue
        title = te.text or "untitled"
        src = ce.get("src", "")
        if not src:
            continue
        try:
            src, fragment = _split_href(src)
        except EpubError:
            continue
        items.append((title, src, fragment))
    return ncx_path.parent, items


def _parse_nav(nav_path):
    try:
        tree = ET.parse(nav_path)
    except ET.ParseError:
        return nav_path.parent, []
    root = tree.getroot()
    navs = [el for el in root.iter() if _local_name(el.tag) == "nav"]
    nav_el = None
    for candidate in navs:
        for attr_name, attr_val in candidate.attrib.items():
            if _local_name(attr_name) == "type" and "toc" in attr_val:
                nav_el = candidate
                break
        if nav_el is not None:
            break
    if nav_el is None and navs:
        nav_el = navs[0]
    if nav_el is None:
        return nav_path.parent, []
    items = []

    def walk(node):
        for child in node:
            name = _local_name(child.tag)
            if name in ("ol", "ul"):
                walk(child)
            elif name == "li":
                a_el = None
                for sub in child.iter():
                    if _local_name(sub.tag) == "a":
                        a_el = sub
                        break
                if a_el is not None:
                    href = a_el.attrib.get("href", "")
                    if href:
                        try:
                            src, fragment = _split_href(href)
                        except EpubError:
                            continue
                        text = "".join(a_el.itertext()).strip()
                        title = text or "untitled"
                        items.append((title, src, fragment))
                for sub in child:
                    if _local_name(sub.tag) in ("ol", "ul"):
                        walk(sub)

    walk(nav_el)
    return nav_path.parent, items


def _find_toc(root):
    opf, manifest, spine_el = _parse_opf(root)
    if opf is None:
        return None, []

    nav_item = None
    for it in manifest.values():
        props = it.attrib.get("properties", "")
        if "nav" in props.split():
            nav_item = it
            break
    if nav_item is not None:
        nav_path = None
        href = nav_item.attrib.get("href", "")
        if href:
            try:
                nav_path = _safe_child(root, opf.parent, href)
            except EpubError:
                nav_path = None
        if nav_path is not None:
            base_dir, items = _parse_nav(nav_path)
            if items:
                return base_dir, items

    ncx_item = None
    if spine_el is not None:
        toc_id = spine_el.attrib.get("toc")
        if toc_id and toc_id in manifest:
            ncx_item = manifest[toc_id]
    if ncx_item is None:
        for it in manifest.values():
            if it.attrib.get("media-type") == "application/x-dtbncx+xml":
                ncx_item = it
                break
    if ncx_item is not None:
        ncx_path = None
        href = ncx_item.attrib.get("href", "")
        if href:
            try:
                ncx_path = _safe_child(root, opf.parent, href)
            except EpubError:
                ncx_path = None
        if ncx_path is not None:
            base_dir, items = _parse_ncx(ncx_path)
            if items:
                return base_dir, items

    return None, []


def _find_spine(root):
    opf, manifest, spine_el = _parse_opf(root)
    if opf is None or spine_el is None:
        return None, []

    items = []
    for ref in spine_el:
        if _local_name(ref.tag) != "itemref":
            continue
        item = manifest.get(ref.attrib.get("idref", ""))
        if item is None:
            continue
        href = item.attrib.get("href", "")
        media_type = item.attrib.get("media-type", "")
        if not href or "html" not in media_type:
            continue
        try:
            src, _fragment = _split_href(href)
        except EpubError:
            continue
        items.append(src)
    return opf.parent, items


def _extract_title(path):
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return None
    for tag_name in ("h1", "h2", "h3"):
        for el in tree.getroot().iter():
            if _local_name(el.tag) != tag_name:
                continue
            text = " ".join("".join(el.itertext()).split())
            if text:
                return text
    return None


def _build_toc_chapters(root, base_dir, items):
    chapters = []
    seen = set()
    for order, item in enumerate(items, 1):
        title, src, fragment = item
        try:
            html_path = _safe_child(root, base_dir, src)
        except EpubError:
            continue
        if html_path.suffix.lower() not in (".xhtml", ".html", ".htm"):
            continue
        if not html_path.exists():
            continue
        key = (str(html_path.resolve()), fragment or "")
        if key in seen:
            continue
        seen.add(key)
        chapters.append(
            {
                "order": order,
                "title": title,
                "html_path": html_path,
                "fragment": fragment,
                "start_id": None,
                "end_id": None,
            }
        )
    return chapters


def _build_spine_chapters(root, base_dir, items):
    chapters = []
    seen = set()
    for order, src in enumerate(items, 1):
        try:
            html_path = _safe_child(root, base_dir, src)
        except EpubError:
            continue
        if html_path.suffix.lower() not in (".xhtml", ".html", ".htm"):
            continue
        if not html_path.exists():
            continue
        key = str(html_path.resolve())
        if key in seen:
            continue
        seen.add(key)
        chapters.append(
            {
                "order": order,
                "title": _extract_title(html_path) or Path(src).stem,
                "html_path": html_path,
                "fragment": None,
                "start_id": None,
                "end_id": None,
            }
        )
    return chapters


def _spine_html_paths(root, base_dir, items):
    paths = set()
    if base_dir is None:
        return paths
    for src in items:
        try:
            html_path = _safe_child(root, base_dir, src)
        except EpubError:
            continue
        if html_path.exists():
            paths.add(str(html_path.resolve()))
    return paths


def _assign_fragment_ranges(chapters):
    by_file = {}
    for chapter in chapters:
        key = str(chapter["html_path"].resolve())
        by_file.setdefault(key, []).append(chapter)

    for group in by_file.values():
        group.sort(key=lambda chapter: chapter["order"])
        if not any(chapter["fragment"] for chapter in group):
            continue
        for index, chapter in enumerate(group):
            next_fragment = next(
                (
                    later["fragment"]
                    for later in group[index + 1 :]
                    if later["fragment"]
                ),
                None,
            )
            if chapter["fragment"]:
                chapter["start_id"] = chapter["fragment"]
                chapter["end_id"] = next_fragment
            elif index == 0 and next_fragment:
                chapter["end_id"] = next_fragment


def _find_anchor(text, anchor):
    if not anchor:
        return None
    for match in re.finditer(r"\b(?:id|name)\s*=\s*(['\"])(.*?)\1", text, re.I):
        if match.group(2) != anchor:
            continue
        start = text.rfind("<", 0, match.start())
        return start if start != -1 else match.start()
    return None


def _extract_segment(text, start_id, end_id):
    if not start_id and not end_id:
        return None
    start = _find_anchor(text, start_id) if start_id else 0
    if start is None:
        return None
    end = len(text)
    if end_id:
        end_pos = _find_anchor(text, end_id)
        if end_pos is not None and end_pos > start:
            end = end_pos
    segment = text[start:end].strip()
    return segment or None


def _chapter_input(chapter, prepared_path, work_dir):
    if chapter["start_id"] is None and chapter["end_id"] is None:
        return prepared_path
    try:
        text = prepared_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return prepared_path
    segment = _extract_segment(text, chapter["start_id"], chapter["end_id"])
    if segment is None:
        return prepared_path
    target = work_dir / f"{len(list(work_dir.iterdir())):05d}-segment.xhtml"
    target.write_text(segment, encoding="utf-8")
    return target


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ["-h", "--help"]:
        print(
            "epub2md - Convert EPUB to Markdown\n\n"
            "Usage: epub2md <book.epub> [outdir]\n\n"
            "Output:\n"
            "  <outdir>/*.md: Markdown files\n"
            "  <outdir>/images/: Images"
        )
        sys.exit(0)

    if len(sys.argv) > 3:
        sys.exit("Error: too many arguments")

    epub = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2] if len(sys.argv) > 2 else epub.stem).resolve()

    if not epub.exists():
        sys.exit(f"Error: {epub} not found")
    if not shutil.which("pandoc"):
        sys.exit("Error: pandoc not found. Install: brew install pandoc")

    print(f"Converting {epub.name}...")
    out.mkdir(exist_ok=True)
    media = out / "images"
    media.mkdir(exist_ok=True)
    (media / ".gitignore").write_text("*\n")

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        book = t / "book"
        book.mkdir()
        try:
            _extract_epub(epub, book)
        except EpubError as exc:
            sys.exit(f"Error: {exc}")
        lua = t / "f.lua"
        lua.write_text(LUA)
        html_work = t / "html"
        html_work.mkdir()
        copied_images = {}
        used_image_names = set()

        base_dir, items = _find_toc(book)
        spine_dir, spine_files = _find_spine(book)
        chapters = []
        use_spine = False
        if base_dir is not None and items:
            chapters = _build_toc_chapters(book, base_dir, items)
            spine_paths = _spine_html_paths(book, spine_dir, spine_files)
            toc_paths = {str(chapter["html_path"].resolve()) for chapter in chapters}
            if spine_paths and len(toc_paths) < len(spine_paths) * 0.5:
                print(
                    f"TOC covers {len(toc_paths)}/{len(spine_paths)} spine files, using spine instead"
                )
                use_spine = True
        else:
            use_spine = True

        if use_spine or not chapters:
            if spine_dir is None or not spine_files:
                sys.exit("Error: no toc or spine found")
            chapters = _build_spine_chapters(book, spine_dir, spine_files)
            if not chapters:
                sys.exit("Error: no html chapters found")
            print(f"Using spine: {len(chapters)} files")
        else:
            _assign_fragment_ranges(chapters)
            print(f"Found {len(items)} entries in toc")

        n = 0
        failures = 0
        for chapter in sorted(chapters, key=lambda item: item["order"]):
            pandoc_input = _prepare_html_for_pandoc(
                book,
                chapter["html_path"],
                media,
                html_work,
                copied_images,
                used_image_names,
            )
            pandoc_input = _chapter_input(chapter, pandoc_input, html_work)
            safe = _safe_output_slug(chapter["title"])
            name = out / f"{n + 1:02d}-{safe}.md"

            r = subprocess.run(
                [
                    "pandoc",
                    "--sandbox",
                    "-f",
                    "html",
                    "-t",
                    "gfm",
                    "--wrap=none",
                    "--lua-filter",
                    str(lua),
                    "-o",
                    str(name),
                    "--",
                    str(pandoc_input),
                ],
                capture_output=True,
                text=True,
            )

            if r.returncode == 0:
                n += 1
                print(f"✓ {n:02d} {chapter['title']}")
            else:
                failures += 1
                print(f"✗ {chapter['title']}")
                if r.stderr:
                    print(f"  Error: {r.stderr[:200]}")

        if failures:
            sys.exit(f"Error: {failures} chapter conversion(s) failed")

    print(f"\nDone! {n} chapters → {out}/")
    if media.exists():
        imgs = [p for p in media.rglob("*") if p.is_file() and p.name != ".gitignore"]
        if imgs:
            print(f"{len(imgs)} images → {media}/")


if __name__ == "__main__":
    main()
