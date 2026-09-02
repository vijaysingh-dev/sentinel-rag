# Ground truth for parser evaluation

One JSON file per source document, named after the document stem. `methodology.pdf` pairs with
`methodology.json`.

## Format

```json
{
  "text": "The full expected text, in reading order.",
  "tables": [
    [["header1", "header2"], ["row1col1", "row1col2"]]
  ],
  "blocks": ["First block text", "Second block text"],
  "headings": ["Section One", "Section Two"]
}
```

Every field is optional; `evaluation/parsing_eval.py` scores only what is present, so a file with
just `tables` is a valid table-accuracy-only case.

| Field | Scores |
|---|---|
| `text` | normalised edit similarity — did the characters survive |
| `tables` | cell-level F1 (simplified TEDS) — did the grid survive |
| `blocks` | Kendall's tau on positions — did reading order survive |
| `headings` | recall — was structure recovered |

## Why hand-label at all

Without ground truth, `parser_bakeoff.py` falls back to the reference-free heuristics in
`parsing/quality.py`. Those are good enough to *route* a document at ingest but not good enough to
*choose a provider*, because they cannot detect the failure that matters most: reading-order
scrambling in multi-column layouts, where every character is correct and the text is unreadable.

## How many

Ten to twenty documents chosen to span the corpus is enough to rank providers. Weight toward the
hard cases — scanned pages, multi-column layouts, dense tables — since every parser handles a clean
single-column PDF and those documents carry no signal.

## Producing them

The practical route is to run the strongest available parser, then correct its output by hand.
Faster than transcribing from scratch, and the corrections are exactly the failures worth measuring.

<!-- corpus-provenance -->
## Provenance of the generated files

Every `<stem>.json` in this directory is produced by `scripts/corpus/gen_ground_truth.py` from
the same model (`scripts/corpus/services.yaml`) that generates the documents in `knowledge/`, so
document and truth stay in lockstep — edit the model, run `make corpus`, both update.

The one genuinely hand-labelled file is `escalation-card-scanned.json`: that document is an image
with no machine-readable source, so its `text` is transcribed by hand (`SCANNED_CARD_TEXT` in the
generator) and is the human reference the OCR parsers are scored against.

Two non-standard keys appear in some files — `must_contain` and `must_not_contain` — used by
`tests/` to assert boilerplate (running headers) is dropped and key facts survive. `parsing_eval.py`
ignores them; it reads only `text`, `tables`, `blocks`, `headings`.
