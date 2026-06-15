# epub2md

Convert EPUB books into Markdown chapter files.

`epub2md` is a small CLI for turning an EPUB navigation tree into readable
GitHub-flavored Markdown, with local EPUB images copied beside the output.

| Source | Markdown | Images |
| --- | --- | --- |
| `book.epub` | `book/*.md` | `book/images/*` |
| `book.epub out` | `out/*.md` | `out/images/*` |

## Install

```bash
pip install epub2md
```

Requires Python 3.8+ and [pandoc](https://pandoc.org/installing.html).

## Use

```bash
epub2md book.epub
epub2md book.epub output
```

```text
output/
├── 01-introduction.md
├── 02-chapter-1.md
├── 03-chapter-2.md
└── images/
    ├── figure-1.png
    └── .gitignore
```

Images are git-ignored by default. Remove `output/images/.gitignore` if you
want generated images committed with the Markdown.

## Behavior

| Area | Contract |
| --- | --- |
| Chapters | Uses EPUB `nav.xhtml` or NCX table-of-contents order |
| Filenames | Prefixes files with stable numeric order |
| Markdown | Emits GitHub-flavored Markdown through pandoc |
| Images | Copies EPUB-local images to `images/` and rewrites image refs |
| Remote media | Not fetched |
| Safety | Rejects archive traversal, symlinks, oversized archives, and unsafe paths |

## Security

EPUB files are treated as untrusted archives.

| Risk | Control |
| --- | --- |
| ZIP path traversal | Archive paths are validated before extraction |
| Archive symlinks | Rejected before conversion |
| Host file reads | TOC and image paths must resolve inside the EPUB |
| Pandoc option injection | Chapter paths are passed after `--` |
| Pandoc file access | Runs with `--sandbox` |

## Limits

| Current limit | Notes |
| --- | --- |
| Cross-chapter links | May remain as original EPUB `.xhtml` hrefs |
| Nested TOC entries | Multiple entries pointing to the same XHTML file become one Markdown file |
| Image formats | EPUB-local files are copied as-is |
| Layout fidelity | Output favors readable Markdown over exact EPUB layout |

## Development

```bash
python3 -m unittest discover -v
uv build
```

## License

MIT
