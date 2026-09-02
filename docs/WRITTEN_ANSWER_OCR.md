# Written-Answer OCR

Written-answer OCR is text extraction, not grading. The grading/review layer
must still decide whether extracted text is usable.

## Recommended Architecture

```text
canonical aligned page
  -> manifest written_block boxes
  -> exact answer crop per question
  -> split crop into expected ruled lines
  -> remove printed box/rule lines
  -> normalize illumination and resize line crop
  -> offline handwriting text recognizer
  -> line text + confidence + review flags
  -> question-level extracted text
```

This keeps geometry deterministic. The system does not search the page for
answers after alignment; it uses the manifest coordinates, then treats OCR as
an optional interpretation of the saved crop.

## Backends

- `trocr`: best current local option for English handwritten line OCR. It uses
  `microsoft/trocr-base-handwritten` by default and runs through
  Hugging Face Transformers. It needs `python -m pip install .[htr]`.
- `tesseract`: lightweight local fallback. Useful for neat writing and printed
  text, but not reliable enough for poor handwriting.
- Future backend: PaddleOCR can be added for text detection, orientation, and
  recognition if we want a broader OCR pipeline.

## Why Line Segmentation Matters

The generated OMR already knows how many ruled lines each answer has. Splitting
the crop into line images makes the OCR task much easier than giving one large
answer box to a recognizer. TrOCR-style models are strongest on single-line
handwritten text, so each written line is processed independently and then
joined back into the question answer.

## Current Parameters

- Written line target height: `72 px`
- Minimum line ink fraction: `0.0025`
- Rule removal: horizontal morphology kernel `max(24, width / 5)`
- Vertical border removal: vertical morphology kernel `max(10, height / 2)`
- TrOCR default model: `microsoft/trocr-base-handwritten`
- TrOCR generation: `num_beams=4`, `max_new_tokens=96`

## Production Caution

Very poor handwriting cannot be made perfectly accurate by threshold changes
alone. Accuracy needs three things:

- clean aligned crops
- a recognizer trained or fine-tuned on handwriting similar to our students
- confidence/roster/rubric validation before accepting the result

So the correct behavior is: extract text where confidence is good, save crops
and flags where it is not, and never silently grade doubtful handwriting.

## References

- TrOCR paper:
  https://arxiv.org/abs/2109.10282
- Hugging Face TrOCR model docs:
  https://huggingface.co/docs/transformers/en/model_doc/trocr
- TrOCR handwritten model:
  https://huggingface.co/microsoft/trocr-base-handwritten
- PaddleOCR pipeline docs:
  https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md
- Tesseract OCR:
  https://github.com/tesseract-ocr/tesseract
