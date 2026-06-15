import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import epub2md


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
    b"\x02\xfeA\xe2!\xbc\x00\x00\x00\x00IEND\xaeB`\x82"
)


class Epub2MdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_epub(
        self,
        name="book.epub",
        toc_entries=None,
        files=None,
        symlinks=None,
        spine_entries=None,
        include_nav=True,
    ):
        if toc_entries is None:
            toc_entries = [("Chapter", "xhtml/chapter.xhtml")]
        files = files or {
            "OEBPS/xhtml/chapter.xhtml": (
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                "<body><h1>Chapter</h1><p>Hello</p></body></html>"
            )
        }
        epub = self.root / name
        nav_items = "\n".join(
            f'<li><a href="{href}">{title}</a></li>' for title, href in toc_entries
        )
        item_ids = {}
        package_items = []
        for i, path in enumerate(files):
            if not path.endswith((".xhtml", ".html", ".htm")):
                continue
            href = path[6:] if path.startswith("OEBPS/") else path
            item_id = f"item{i}"
            item_ids[path] = item_id
            item_ids[href] = item_id
            package_items.append(
                f'<item id="{item_id}" href="{href}" media-type="application/xhtml+xml"/>'
            )
        if include_nav:
            package_items.insert(
                0,
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            )
        package_items = "\n                        ".join(package_items)
        spine_items = "\n                        ".join(
            f'<itemref idref="{item_ids[path]}"/>'
            for path in (spine_entries or [])
            if path in item_ids
        )

        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr(
                "META-INF/container.xml",
                textwrap.dedent(
                    """\
                    <?xml version="1.0"?>
                    <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                      <rootfiles>
                        <rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/>
                      </rootfiles>
                    </container>
                    """
                ),
            )
            zf.writestr(
                "OEBPS/package.opf",
                textwrap.dedent(
                    f"""\
                    <?xml version="1.0" encoding="utf-8"?>
                    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
                      <manifest>
                        {package_items}
                      </manifest>
                      <spine>
                        {spine_items}
                      </spine>
                    </package>
                    """
                ),
            )
            if include_nav:
                zf.writestr(
                    "OEBPS/nav.xhtml",
                    textwrap.dedent(
                        f"""\
                        <html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
                          <body><nav epub:type="toc"><ol>{nav_items}</ol></nav></body>
                        </html>
                        """
                    ),
                )
            for path, content in files.items():
                if isinstance(content, bytes):
                    zf.writestr(path, content)
                else:
                    zf.writestr(path, content)
            for path, target in (symlinks or {}).items():
                info = zipfile.ZipInfo(path)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, target)
        return epub

    def fake_pandoc(self, exit_code=0):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        log = self.root / "pandoc.jsonl"
        script = bin_dir / "pandoc"
        script.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json, os, pathlib, sys
                args = sys.argv[1:]
                pathlib.Path(os.environ["PANDOC_LOG"]).open("a").write(json.dumps(args) + "\\n")
                if "-o" in args:
                    out = pathlib.Path(args[args.index("-o") + 1])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    content = "converted\\n"
                    if "--" in args and args.index("--") < len(args) - 1:
                        inp = pathlib.Path(args[args.index("--") + 1])
                        try:
                            content = inp.read_text(encoding="utf-8", errors="ignore")
                        except OSError:
                            pass
                    out.write_text(content)
                raise SystemExit({exit_code})
                """
            )
        )
        script.chmod(0o755)
        return bin_dir, log

    def run_cli(self, epub, out, fake=True, fake_exit=0):
        old_argv = sys.argv[:]
        old_path = os.environ.get("PATH", "")
        old_log = os.environ.get("PANDOC_LOG")
        stdout = io.StringIO()
        code = None
        if fake:
            bin_dir, log = self.fake_pandoc(fake_exit)
            os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
            os.environ["PANDOC_LOG"] = str(log)
        else:
            log = None
        try:
            sys.argv = ["epub2md", str(epub), str(out)]
            with contextlib.redirect_stdout(stdout):
                try:
                    epub2md.main()
                except SystemExit as exc:
                    code = exc.code
        finally:
            sys.argv = old_argv
            os.environ["PATH"] = old_path
            if old_log is None:
                os.environ.pop("PANDOC_LOG", None)
            else:
                os.environ["PANDOC_LOG"] = old_log
        return code, stdout.getvalue(), log

    def read_pandoc_args(self, log):
        return [json.loads(line) for line in log.read_text().splitlines()]

    def test_converts_normal_epub_with_sandbox_and_delimiter(self):
        epub = self.make_epub()
        out = self.root / "out"

        code, stdout, log = self.run_cli(epub, out)

        self.assertIsNone(code)
        self.assertIn("Done! 1 chapters", stdout)
        self.assertTrue((out / "01-chapter.md").exists())
        args = self.read_pandoc_args(log)[0]
        self.assertIn("--sandbox", args)
        self.assertIn("--", args)
        self.assertLess(args.index("--"), len(args) - 1)

    def test_uses_spine_when_toc_is_missing(self):
        files = {
            "OEBPS/xhtml/one.xhtml": (
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                "<body><h1>First Heading</h1><p>One</p></body></html>"
            ),
            "OEBPS/xhtml/two.xhtml": (
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                "<body><h1>Second Heading</h1><p>Two</p></body></html>"
            ),
        }
        epub = self.make_epub(
            toc_entries=[],
            files=files,
            spine_entries=["OEBPS/xhtml/one.xhtml", "OEBPS/xhtml/two.xhtml"],
            include_nav=False,
        )
        out = self.root / "out"

        code, stdout, log = self.run_cli(epub, out)

        self.assertIsNone(code)
        self.assertIn("Using spine: 2 files", stdout)
        self.assertTrue((out / "01-first-heading.md").exists())
        self.assertTrue((out / "02-second-heading.md").exists())
        self.assertEqual(len(self.read_pandoc_args(log)), 2)

    def test_uses_spine_when_toc_coverage_is_too_low(self):
        files = {
            f"OEBPS/xhtml/{name}.xhtml": (
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                f"<body><h1>{title}</h1><p>{title}</p></body></html>"
            )
            for name, title in [
                ("one", "One"),
                ("two", "Two"),
                ("three", "Three"),
                ("four", "Four"),
            ]
        }
        epub = self.make_epub(
            toc_entries=[("Only One", "xhtml/one.xhtml")],
            files=files,
            spine_entries=[
                "OEBPS/xhtml/one.xhtml",
                "OEBPS/xhtml/two.xhtml",
                "OEBPS/xhtml/three.xhtml",
                "OEBPS/xhtml/four.xhtml",
            ],
        )
        out = self.root / "out"

        code, stdout, _log = self.run_cli(epub, out)

        self.assertIsNone(code)
        self.assertIn("TOC covers 1/4 spine files, using spine instead", stdout)
        self.assertIn("Done! 4 chapters", stdout)
        self.assertTrue((out / "01-one.md").exists())
        self.assertTrue((out / "04-four.md").exists())

    def test_fragment_toc_splits_one_html_file_into_segments(self):
        epub = self.make_epub(
            toc_entries=[
                ("Intro", "xhtml/chapter.xhtml"),
                ("Part A", "xhtml/chapter.xhtml#a"),
                ("Part B", "xhtml/chapter.xhtml#b"),
            ],
            files={
                "OEBPS/xhtml/chapter.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml">'
                    "<body>"
                    "<p>Intro body</p>"
                    '<h2 id="a">A</h2><p>A body</p>'
                    '<h2 id="b">B</h2><p>B body</p>'
                    "</body></html>"
                )
            },
        )
        out = self.root / "out"

        code, stdout, _log = self.run_cli(epub, out)

        self.assertIsNone(code)
        self.assertIn("Done! 3 chapters", stdout)
        intro = (out / "01-intro.md").read_text()
        part_a = (out / "02-part-a.md").read_text()
        part_b = (out / "03-part-b.md").read_text()
        self.assertIn("Intro body", intro)
        self.assertNotIn("A body", intro)
        self.assertIn("A body", part_a)
        self.assertNotIn("B body", part_a)
        self.assertIn("B body", part_b)

    def test_fragment_segments_keep_rewritten_image_links(self):
        epub = self.make_epub(
            toc_entries=[
                ("Part A", "xhtml/chapter.xhtml#a"),
                ("Part B", "xhtml/chapter.xhtml#b"),
            ],
            files={
                "OEBPS/xhtml/chapter.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml">'
                    "<body>"
                    '<h2 id="a">A</h2><img src="../images/pic.png" alt="pic"/>'
                    '<h2 id="b">B</h2><p>B body</p>'
                    "</body></html>"
                ),
                "OEBPS/images/pic.png": PNG_BYTES,
            },
        )
        out = self.root / "out"

        code, _stdout, _log = self.run_cli(epub, out)

        self.assertIsNone(code)
        self.assertEqual((out / "images" / "pic.png").read_bytes(), PNG_BYTES)
        self.assertIn("images/pic.png", (out / "01-part-a.md").read_text())

    def test_long_output_slug_is_truncated(self):
        title = "Very Long Chapter Title " * 8
        epub = self.make_epub(toc_entries=[(title, "xhtml/chapter.xhtml")])
        out = self.root / "out"

        code, _stdout, _log = self.run_cli(epub, out)

        self.assertIsNone(code)
        expected = epub2md._safe_output_slug(title)
        self.assertLessEqual(len(expected), epub2md.MAX_SLUG_LENGTH)
        self.assertTrue((out / f"01-{expected}.md").exists())

    def test_option_like_toc_path_is_pandoc_file_argument(self):
        epub = self.make_epub(
            toc_entries=[("Chapter", "--lua-filter=evil.xhtml")],
            files={
                "OEBPS/--lua-filter=evil.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml">'
                    "<body><h1>Chapter</h1></body></html>"
                )
            },
        )
        out = self.root / "out"

        code, _stdout, log = self.run_cli(epub, out)

        self.assertIsNone(code)
        args = self.read_pandoc_args(log)[0]
        self.assertEqual(args.count("--lua-filter"), 1)
        self.assertEqual(args[args.index("--") + 1], args[-1])
        self.assertTrue(args[-1].endswith("--lua-filter=evil.xhtml"))

    def test_archive_symlink_is_rejected_before_conversion(self):
        victim = self.root / "victim.txt"
        victim.write_text("do not overwrite")
        epub = self.make_epub(symlinks={"f.lua": str(victim)})
        out = self.root / "out"

        code, _stdout, log = self.run_cli(epub, out)

        self.assertIn("archive symlinks are not allowed", str(code))
        self.assertEqual(victim.read_text(), "do not overwrite")
        self.assertFalse(log.exists())

    def test_toc_path_traversal_is_rejected(self):
        epub = self.make_epub(toc_entries=[("Secret", "../secret.xhtml")])
        out = self.root / "out"

        code, _stdout, log = self.run_cli(epub, out)

        self.assertEqual(str(code), "Error: no toc or spine found")
        self.assertFalse(log.exists())

    def test_archive_path_traversal_is_rejected(self):
        epub = self.root / "traversal.epub"
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr("../outside.txt", "escape")

        with self.assertRaisesRegex(epub2md.EpubError, "unsafe archive path"):
            epub2md._extract_epub(epub, self.root / "extract")

    def test_archive_size_limit_is_enforced(self):
        epub = self.root / "oversized.epub"
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr("big.txt", "abcd")

        old_limit = epub2md.MAX_ARCHIVE_BYTES
        try:
            epub2md.MAX_ARCHIVE_BYTES = 3
            with self.assertRaisesRegex(epub2md.EpubError, "too large"):
                epub2md._extract_epub(epub, self.root / "extract")
        finally:
            epub2md.MAX_ARCHIVE_BYTES = old_limit

    def test_gitignore_only_is_not_reported_as_image(self):
        epub = self.make_epub()
        out = self.root / "out"

        code, stdout, _log = self.run_cli(epub, out)

        self.assertIsNone(code)
        self.assertNotIn("images \u2192", stdout)

    @unittest.skipUnless(shutil.which("pandoc"), "pandoc is required for integration test")
    def test_relative_epub_image_is_copied_and_linked(self):
        epub = self.make_epub(
            files={
                "OEBPS/xhtml/chapter.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml">'
                    '<body><h1>Chapter</h1><img src="../images/pic.png" alt="pic"/></body></html>'
                ),
                "OEBPS/images/pic.png": PNG_BYTES,
            }
        )
        out = self.root / "out"

        code, stdout, _log = self.run_cli(epub, out, fake=False)

        self.assertIsNone(code)
        self.assertIn("1 images \u2192", stdout)
        self.assertEqual((out / "images" / "pic.png").read_bytes(), PNG_BYTES)
        md = (out / "01-chapter.md").read_text()
        self.assertIn("images/pic.png", md)

    @unittest.skipUnless(shutil.which("pandoc"), "pandoc is required for integration test")
    def test_option_like_toc_path_does_not_execute_lua_filter(self):
        marker = self.root / "marker.txt"
        lua = f"io.open({str(marker)!r}, 'w'):write('executed')"
        epub = self.make_epub(
            toc_entries=[("Chapter", "--lua-filter=tmp/evil.xhtml")],
            files={
                "OEBPS/--lua-filter=tmp/evil.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml">'
                    "<body><h1>Chapter</h1></body></html>"
                ),
                "OEBPS/tmp/evil.xhtml": lua,
            },
        )
        out = self.root / "out"

        code, _stdout, _log = self.run_cli(epub, out, fake=False)

        self.assertIsNone(code)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(shutil.which("pandoc"), "pandoc is required for integration test")
    def test_absolute_image_reference_is_not_copied_by_pandoc(self):
        secret = self.root / "secret.png"
        secret.write_bytes(PNG_BYTES + b"SECRET_IMAGE_BYTES")
        epub = self.make_epub(
            files={
                "OEBPS/xhtml/chapter.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml">'
                    f'<body><h1>Chapter</h1><img src="{secret}" alt="secret"/></body></html>'
                )
            }
        )
        out = self.root / "out"

        code, _stdout, _log = self.run_cli(epub, out, fake=False)

        self.assertIsNone(code)
        copied = b"".join(
            path.read_bytes()
            for path in (out / "images").rglob("*")
            if path.is_file() and path.name != ".gitignore"
        )
        md = (out / "01-chapter.md").read_text()
        self.assertNotIn(b"SECRET_IMAGE_BYTES", copied)
        self.assertNotIn(str(secret), md)


if __name__ == "__main__":
    unittest.main()
