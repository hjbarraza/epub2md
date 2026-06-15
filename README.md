# epub2md

EPUB to Markdown chapters.

| Input | Output | Images |
| --- | --- | --- |
| `book.epub` | `book/*.md` | `book/images/*` |
| `book.epub output` | `output/*.md` | `output/images/*` |

## Install

```bash
pip install epub2md
```

## Use

```bash
epub2md book.epub
epub2md book.epub output
```

```text
output/
├── 01-chapter.md
├── 02-chapter.md
├── ...
└── images/
    ├── figure.png
    └── .gitignore
```

## Details

| Area | Behavior |
| --- | --- |
| Chapters | Uses EPUB navigation/TOC order |
| Markdown | Writes GitHub-flavored Markdown |
| Images | Copies EPUB-local images into `images/` |
| Safety | Rejects archive traversal, symlinks, and unsafe paths |

Images are git-ignored by default. Remove `output/images/.gitignore` to commit them.

## Requirements

| Runtime | External |
| --- | --- |
| Python 3.8+ | [pandoc](https://pandoc.org/installing.html) |

## License

MIT
