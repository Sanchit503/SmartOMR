# SmartOMR: Answer Extraction and AI Grading Decision

Research date: 2026-09-06. Hardware/setup update: 2026-09-07. Status: proposed improvements to the existing OCR pipeline and proposed real AI grading; the new architecture has not been implemented.

Inputs reviewed: the supplied `handwritten_text_recognition_deep_research_2026.docx`, the accompanying pasted brief, the existing written-answer implementation and tests, and the primary sources linked below. The DOCX is research material, not a replacement for the user's instructions or the repository specification.

The user confirmed that the initial pilot is **short English answers, with some numbers or formulas**. The other laptop was subsequently identified as an **RTX 4050 Laptop GPU with 6 GB VRAM and 16 GB system RAM**, correcting the earlier RTX 4060 assumption. The college can provide more capable hardware, but its exact server configuration is still unspecified. No handwriting inference benchmark or training was performed during this investigation. The user later reported successful model loading on the other laptop; that is not a transcription test (Section 11).

For the immediate tester handoff, use the existing [full-sheet pilot workflow](WRITTEN_ANSWER_OCR.md#full-sheet-pilot): full scanned OMRs plus their original matching manifest. The current batch command already crops answers and prepares lines automatically. Manually cropped lines are an optional diagnostic for separating segmentation errors from recognition errors, not a prerequisite for testing full exams.

## 1. Recommended Decision

Use a **local, separately evaluated extraction and grading pipeline**. Preserve the image and literal transcription, use an independent reader for uncertain extraction, and apply one fixed marking standard per question.

| Decision | Recommendation for SmartOMR |
| --- | --- |
| Initial primary for short English prose | `microsoft/trocr-base-handwritten`, using actual detected text lines. This is a provisional pilot choice based on task fit, integration cost, and hardware, not a proven accuracy winner. |
| Compact second reader and formula route | `PaddlePaddle/PaddleOCR-VL-1.6`, operating on the whole answer crop or formula region. Benchmark it as a primary challenger too. |
| Independent general visual challenger | `Qwen/Qwen3-VL-8B-Instruct`. Test whole-answer literal transcription separately from its reasoning abilities. |
| Additional low-cost challengers | TrOCR-large, `zai-org/GLM-OCR`, and `PaddlePaddle/PP-OCRv6_medium_rec`. |
| Larger-laptop grading candidate, not the 6 GB pilot | `Qwen/Qwen3.5-9B`, quantized to approximately 4-bit, with short bounded requests on a separately validated 8-12 GB or larger setup. Keep the actual 6 GB laptop focused on extraction initially. |
| College grading candidate | `Qwen/Qwen3.8-27B`; compare with `google/gemma-4-31B-it` on human-marked answers. Choose the more accurate calibrated grader, not the newest model by default. |
| Marking standard | Professor-supplied rubric/reference when available. Otherwise draft a question-level solution and rubric once, have the professor validate it, and freeze it before grading. |
| Immediate implementation milestone after this investigation | An evaluation harness and extraction-quality fixes, followed by a benchmark of real answer crops. Production model selection follows the results. |

The preferred overall approach and the preferred fully local approach are the same at this stage. There is no project-specific evidence that paid inference is necessary. A cloud reader can be an optional benchmark later if local candidates leave too much difficult work for reviewers.

For plain English, start with the existing dedicated recognizer because its interface and intended input already fit. For formulas, retain two-dimensional structure and use the compact visual reader. If the whole-answer reader outperforms the line pipeline at the same accepted-answer error rate, promote it. The architecture supports that change without replacing the scanner or grouping system.

The model cards establish input formats and available implementations: [TrOCR-base](https://huggingface.co/microsoft/trocr-base-handwritten), [PaddleOCR-VL-1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6), [Qwen3-VL-8B](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct), [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B), and [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B). The deployment choices above are engineering judgments to test on our data.

## 2. Assessment of the Supplied Research

### What transfers well

The report correctly separates handwriting recognition from generic page OCR, recommends retaining the original image, recognizes that TrOCR expects individual lines, and emphasizes writer-disjoint evaluation. Its warning about comparing different datasets and normalization rules is essential. Its advice to calibrate scores and preserve misspellings is directly relevant to exam answers.

Microsoft's published IAM results are 3.42% cased CER for base and 2.89% for large, with 334M and 558M parameters respectively. These establish a useful within-family comparison. They do not establish SmartOMR accuracy on photographed answers. [Microsoft's TrOCR results](https://github.com/microsoft/unilm/blob/master/trocr/README.md).

The reported PP-OCRv6 handwritten-English result of 67.8% is present in Paddle's internal recognition table. It is recognition accuracy on that benchmark, not CER, and not a claim about our students. The same documentation reports speed under specified hardware; those numbers cannot be transplanted to a laptop. [PP-OCRv6 metrics](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html).

### What needs updating or qualifying

1. **Add compact document VLMs to the shortlist.** PaddleOCR-VL-1.6 and GLM-OCR can recognize existing cropped regions. A full document-layout pipeline is unnecessary for our manifest-defined boxes. Rejecting all document models as excessive would miss these useful recognition components.
2. **Use local fallback first.** The report's preferred cloud fallback is not required by our brief, particularly with college hardware available.
3. **Treat printed lines as layout metadata.** A student can write between rules, use three lines in a two-line box, or write a fraction spanning both lines. Neither our current equal-band split nor the document's example that keeps only two dominant bands is suitable as a universal extractor.
4. **Do not assume agreement proves correctness.** Two models may share language priors or overlook the same faint minus sign. Calibrate agreement jointly with image quality and critical-token errors.
5. **Do not predeclare fine-tuning as necessary.** First measure off-the-shelf models and preprocessing. Suggested counts such as 500 or 1,000 images are starting budgets, not guarantees.
6. **Add grading evaluation.** The supplied document principally addresses transcription. It does not establish which local model can reliably apply IIIT Delhi marking schemes.
7. **Keep evidence granularity visible.** A benchmark score over full document layouts is not a handwriting exact-match rate. A result on isolated capital letters is not evidence of descriptive-answer grading accuracy.

PaddleOCR-VL-1.6 reports 96.33 on OmniDocBench v1.6, a document-parsing benchmark; this is not 96.33% correct student answers. Its distortion experiments support considering it for phone inputs, but our handwriting benchmark remains necessary. [PaddleOCR-VL-1.6 technical report](https://arxiv.org/html/2606.03264v1).

The newer OmniHandwritingOCR study is particularly informative: its English subset includes IAM lines and GNHK, while other subsets contain mathematical expressions. Qwen3-VL-8B reports 16.44% CER on its English subset. This differs substantially from selected older IAM/page studies and shows why a single headline is insufficient. That paper evaluates the original PaddleOCR-VL and DeepSeek-OCR, not their newer versions; its rankings cannot establish the performance of VL-1.6 or OCR-2. [OmniHandwritingOCR, Tables 4-7](https://arxiv.org/html/2608.18586v1).

The report's named Gemini 3.8 Flash and Claude Sonnet 5 releases could be verified against official pages. Their existence and current pricing do not demonstrate superiority on our handwriting. They remain optional external comparisons. [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [Claude Sonnet 5 release](https://www.anthropic.com/news/claude-sonnet-5).

Some specialist fine-tuning and HTR papers in the DOCX were not independently reproduced. The cited WACV webpage could not be retrieved during this investigation, so its claimed full-fine-tuning versus LoRA outcome is not used as a deciding fact here.

## 3. What the Existing Code Actually Provides

The reusable foundation is already in place:

- Manifest-based crops in [reader/written.py](../omr/reader/written.py).
- Optional line OCR, Tesseract and TrOCR adapters, per-line output, and review flags in [reader/written_ocr.py](../omr/reader/written_ocr.py).
- Verified-student export, question/rubric metadata, manual marks, and combined scores in [workflows/written.py](../omr/workflows/written.py).
- A provider interface with request/result records in [grading/written.py](../omr/grading/written.py). Only a mock grader is implemented.

The manifest supplies locations and maximum marks. It does not necessarily contain the actual question, reference answer, acceptable alternatives, numerical tolerance, or marking criteria. The existing optional rubric CSV is the natural starting point for this separate question metadata.

### Findings to address before real AI grades

| Location | Observed behavior | Consequence and proposed correction |
| --- | --- | --- |
| `written.py:_gray_array` | Saved answer crops use grayscale, including a simple channel mean for color input. | Retain an aligned color crop as evidence and model input; maintain grayscale as a detection branch. |
| `written_ocr.py:save_written_line_crops`, `_line_band` | Splits by `crop.lines` into equal overlapping bands. | Writing can be cut, duplicated, or missed. Detect actual text lines and retain the whole crop for comparison. |
| `written_ocr.py:_trim_answer_box_border` | Removes fixed percentages of the answer image. | Writing near the edge can be removed. Use manifest border geometry and inspect edge ink. |
| `written_ocr.py:_remove_rules_from_gray` | Whitened horizontal and vertical morphology masks are applied to every line. | Mathematical strokes and handwriting can disappear. Keep raw input; use template-aware rule handling only when it improves measured extraction. |
| `written_ocr.py:_generation_confidence` | Takes the maximum probability across all vocabulary entries and beams at each step, then a median. | This does not score the returned sequence. Follow generated tokens and beam ancestry, then calibrate against actual transcription errors. |
| `written_ocr.py:_line_is_blank` | Uses cleaned connected-component area and ink-fraction thresholds. | A faint digit, dot, or mathematical mark may be discarded. Distinguish confirmed blank, ink present, and uncertain presence using raw-image evidence. |
| `written_ocr.py:read_line` | One model call per line; token limit is 96. | No real batching; truncation/repetition need explicit detection and retry/review. |
| `written.py:_request_from_answer` | OCR review flags/confidence are not passed into the grading request metadata. | The future provider cannot enforce extraction acceptance from this request alone. Propagate extraction status and transcript version. |
| `grading/written.py:validate_written_grade_result` | Range comparisons do not explicitly reject nonfinite floats. | `NaN` can pass validation. Require finite marks/maxima, strict types, rubric increments, and deterministic sums. |
| `workflows/written.py:_grade_record_from_result` | Provider `raw` details are not persisted in the grade record. | Store model revision, prompt/rubric version, criterion evidence, and inference configuration before introducing real providers. |

Two read-only diagnostics confirmed specific defects:

```text
Synthetic minus-sign dark pixels before/after rule removal: 300 -> 0
NaN accepted by current grade validator: True
NaN review required: False
```

The minus-sign test used a 200 x 60 grayscale array with a 100 x 3 dark horizontal stroke. It demonstrates a failure mode of the morphology function, not a measured frequency in real student work.

Focused existing tests passed: **15 tests** across reader OCR, grading contracts, and the written workflow. The OCR tests use a fake backend and synthetic strokes. Passing them demonstrates workflow behavior; it does not measure transcription or AI-marking accuracy. No production source changes were made in this investigation.

## 4. Extraction Candidate Comparison

The table distinguishes model capabilities from our suitability assessment. No candidate has measured SmartOMR CER, WER, or exact-match accuracy yet.

| Candidate | Fit for handwriting and answer types | Camera/length considerations | Decision |
| --- | --- | --- | --- |
| TrOCR-base / large handwritten | Dedicated English line HTR; strongest task fit for initial prose. Digits can be transcribed, but mathematical layout is outside the intended line-text task. | Requires good line crops; writer/domain shift and faint ink must be tested. Longer answers need reliable line ordering. | Base primary pilot, large accuracy challenger. [Intended input](https://huggingface.co/microsoft/trocr-large-handwritten). |
| PP-OCRv6 medium | Fast conventional recognition; includes an explicit handwritten-English evaluation. Formula structure needs another path. | Detector can locate text under distortion; recognizer-only operation is enough with reliable line crops. | CPU baseline and possible line locator. [Recognition component](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_rec). |
| PaddleOCR-VL-1.6 | Compact visual OCR with text/formula tasks. Promising whole-answer reader for mixed prose and symbols; handwriting fidelity needs measurement. | Can avoid forced line splitting and handle multiple elements. Use appropriate native task prompts. | First compact fallback and primary challenger. [Model](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6). |
| GLM-OCR | Compact text, formula and table recognition; another independent architecture. | Whole-crop inference possible. Document results alone do not establish phone-handwriting accuracy. | Second compact challenger if the initial comparison is inconclusive. [Model](https://huggingface.co/zai-org/GLM-OCR). |
| Qwen3-VL-8B-Instruct | General VLM with handwriting benchmark evidence; may use context well but can insert plausible text. | Reads an entire short answer and spatial symbols; high-resolution vision increases memory. | First general VLM challenger, with strict literal prompting. [Model](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct). |
| Qwen3.5-9B / Qwen3.8-27B | Native multimodal models with reasoning capability; no verified direct win on our handwriting. | Reuse the grading server for an independently prompted image read when useful. | Compare on difficult cases after the initial extraction shortlist. |
| GOT-OCR-2.0-hf | Approximately 0.6B, OCR-specific text/format recognition. | Supports more structure than line HTR; benchmark fidelity on handwritten symbols. | Reserve challenger, less urgent than newer compact OCR models. [Model](https://huggingface.co/stepfun-ai/GOT-OCR-2.0-hf). |
| DeepSeek-OCR-2 | OCR-focused visual model, approximately 3B class. | Page/reading-order work may add limited value to our small fixed regions; more deployment work. | Reserve comparison for mixed/longer answers. [Model](https://huggingface.co/deepseek-ai/DeepSeek-OCR-2). |
| InternVL3_5-8B | General visual model, about 9B including components; Apache-2.0. | Whole-crop reading and visual reasoning; tile/vision cost must be bounded. | Independent VLM alternative if Qwen errors remain correlated with our other reader. [Model](https://huggingface.co/OpenGVLab/InternVL3_5-8B). |
| MiniCPM-V-4_5 | About 8B, local visual model with quantized runtimes; Apache-2.0. | Good deployment options, but general visual benchmarks do not prove short-answer fidelity. | Alternative to Qwen, not an additional default always-on model. [Model](https://huggingface.co/openbmb/MiniCPM-V-4_5). |
| Surya | Current project describes a 650M OCR model and handwriting examples. | Includes layout and detection, with CPU/GPU serving options. | Optional compact comparison. Code is Apache-2.0; weights have separate conditional terms. [Project](https://github.com/datalab-to/surya). |
| Chandra OCR 2 | Approximately 5B model for document transcription and structure. | Whole-page strengths are not necessarily whole-answer gains. | Reserve accuracy comparison; weight terms differ from unrestricted MIT/Apache models. [Model and terms](https://huggingface.co/datalab-to/chandra-ocr-2). |
| docTR | Detector plus word recognizer; useful document OCR components. | Word splitting adds error opportunities for joined cursive. | General OCR control, no evidence to prefer it as the initial handwriting model. [Project](https://github.com/mindee/doctr). |
| EasyOCR | Convenient general OCR library; handwriting is still listed on the roadmap. | Useful CPU/printed-text control. | Do not prioritize it over dedicated HTR for our pilot. [Project](https://github.com/JaidedAI/EasyOCR). |
| Florence-2-large | Approximately 0.77B general vision model with OCR tasks. | Compact, but not an established dedicated handwriting choice. | Optional baseline after stronger task-specific candidates. [Model](https://huggingface.co/microsoft/Florence-2-large). |
| HTR-VT / DRetHTR | Research handwriting architectures worth monitoring. | Additional environment/training work; evidence needs matched evaluation protocols. | Research track after deployable baselines. [HTR-VT](https://github.com/Intellindust-AI-Lab/HTR-VT), [DRetHTR](https://arxiv.org/abs/2602.17387). |

Donut targets document understanding and PARSeq's established scope is scene text. Neither earns an initial benchmark slot without domain adaptation evidence. Research systems using lexicons or semantic correction also need special scrutiny: replacing a student's incorrect term with the expected technical term can change the grade.

### First experiment, in order

1. Run TrOCR-base and TrOCR-large on identical correctly segmented lines.
2. Run PaddleOCR-VL-1.6 on the corresponding complete answer images, keeping its OCR and formula modes separate where applicable.
3. Run Qwen3-VL-8B-Instruct on the complete answer images, blind to the first readers and the reference answer.
4. Run PP-OCRv6 medium as the inexpensive CPU baseline. Add GLM-OCR if compact-reader performance or deployment remains uncertain.

Compare complete reconstructed answers, not line-level scores for one model against answer-level scores for another. Evaluate the segmentation stage separately using some manually corrected line crops. Run the first three independently on all benchmark answers before deciding whether a confidence fallback actually saves compute without hiding errors.

## 5. Preprocessing for Our Actual Sheets

Preserve both the source scan and an aligned color page. Use the existing fiducial-based alignment and quality report. Avoid a second global warp unless a measured residual needs correction. Four corner points correct a planar perspective transform; they cannot guarantee correction of curved or crumpled paper.

For each written question:

1. Extract the exact manifest region and a separate context crop with a small, bounded margin. Keep page coordinates and image hashes. Prevent a margin from silently absorbing the adjacent question.
2. Check raw-image ink, blur, contrast, and contact with the box edges. Return `blank`, `present`, or `uncertain`; do not turn unreadable writing into an empty answer.
3. Build a temporary grayscale/threshold mask for locating strokes and lines. Preserve original color or grayscale intensities as the recognition input.
4. Use the known blank template or manifest rule positions to distinguish printing from writing. Template subtraction must be an evaluated branch because misregistration and handwriting crossing a rule can erase real ink.
5. For prose HTR, locate actual baselines/line bands. Include ascenders and descenders, maintain reading order, and check unexplained ink outside the selected bands. Use the printed line count as a plausibility hint.
6. For fractions, matrices, superscripts, or mixed prose/formula regions, retain the whole two-dimensional region and route to the visual reader. Do not force these into horizontal text slices.
7. Test mild illumination normalization/CLAHE only when quality measurements suggest a problem. Compare with the unenhanced crop and retain provenance.
8. Use the model's expected processor. Test aspect-preserving padding versus its native resizing; do not assume generic resizing is automatically better than checkpoint preprocessing.

Hard thresholding, strong denoising, arbitrary border trimming, destructive rule removal, and generative super-resolution should not be the only input path. A synthetic enhancement that invents a stroke is especially problematic for `-`, `=`, decimal points, exponents, and `0` versus `O`. Resizing may aid a model but cannot restore information absent from a low-resolution capture.

Overflow should produce a visible review flag and context image. It should not trigger unrestricted crop expansion or attach writing from another answer automatically. This is an extraction-quality feature, separate from the generator's pagination and the grading validator's maximum-mark check.

## 6. Extraction Verification and Confidence

Store raw model scores as scores. A value such as `0.91` is not a 91% probability of a correct answer unless calibration has demonstrated that interpretation for a precisely defined event.

For TrOCR, score the actual generated tokens using generation transitions and beam indices, exclude padding and handle end-of-sequence explicitly. Record length-normalized log probability, weak-token summaries, truncation/repetition flags, and image quality. The Transformers API exposes the appropriate sequence transition calculation. [Generation transition scores](https://huggingface.co/docs/transformers/main/en/main_classes/text_generation).

For an independent visual read, supply the image and transcription instructions only. Avoid including the primary prediction, model answer, or expected number, because these can bias it toward agreement. OCR-specialized models need their native task prompts; a generic lengthy chat prompt can move them outside their supported input distribution.

Combine features on a held-out calibration set: primary score, image-quality flags, line completeness, disagreement, critical-token mismatch, and out-of-scope content. Start with explicit review rules; fit a simple calibrated acceptance model only when enough labeled examples exist. A single median score is inadequate.

Suggested routing:

- High-quality prose with calibrated low extraction risk: accept the primary transcript, with a random audit sample still checked.
- Low score, poor quality, unexpected lines, or formula content: obtain the independent whole-answer read.
- Unresolved disagreement: retain both candidates and ask the reviewer to resolve the image evidence.
- Exact agreement plus good visual evidence: eligible for acceptance only under the empirically validated rule.
- Near agreement involving negation, signs, digits, units, names, or technical terms: review, even if normalized edit distance is small.

Do not create a majority-vote sentence from incompatible fragments. A shared error remains possible even with two agreeing models. During the first pilot, human-review every result while learning which acceptance rules are reliable enough to automate.

## 7. Grading Model Comparison

Grading requires applying the professor's standard consistently. General knowledge, coding, mathematics, and instruction-following benchmarks are useful screening signals; none is an academic-marking benchmark for this project.

| Model | Role and rationale | Limitations / resource class |
| --- | --- | --- |
| `Qwen/Qwen3.5-9B` | Larger-laptop/server candidate for criterion-based short-answer grades and a development baseline, not the initial 6 GB laptop workload. | Quantized deployment with tight context; measure score agreement and quantization changes. |
| `Qwen/Qwen3.8-27B` | Preferred college-server candidate, with controllable thinking and image support for exceptional visual cases. | Approximately 56 GB original weights; quantization or larger GPU required. Its release does not prove superior grading. [Model](https://huggingface.co/Qwen/Qwen3.8-27B). |
| `google/gemma-4-31B-it` | Independent-family server challenger and possible second grader for disputed criteria. | About 63 GB original weights. Current card specifies Apache-2.0; do not infer its terms from older Gemma releases. [Model](https://huggingface.co/google/gemma-4-31B-it). |
| `Qwen/Qwen3.6-27B` | Established predecessor as a reproducibility baseline if already provisioned at college. | Same broad memory tier. No reason to deploy both Qwen versions permanently without measured benefit. [Model](https://huggingface.co/Qwen/Qwen3.6-27B). |
| `mistralai/Ministral-3-14B-Instruct-2512` | Mid-sized alternative with structured-output and visual capabilities; Apache-2.0. | Published FP8 deployment fits the 24 GB class; smaller quantizations need testing. [Model](https://huggingface.co/mistralai/Ministral-3-14B-Instruct-2512). |
| `microsoft/Phi-4-reasoning-plus` | English/math text-grading challenger. | About 14B; reasoning tokens increase latency. Not a handwriting reader or demonstrated grading winner. [Model](https://huggingface.co/microsoft/Phi-4-reasoning-plus). |
| `deepseek-ai/DeepSeek-R1-Distill-Qwen-14B` | Optional mathematical reasoning comparison. | About 14B class; long reasoning and model-specific decoding need evaluation. [Model](https://huggingface.co/deepseek-ai/DeepSeek-R1-Distill-Qwen-14B). |
| Llama 3.3 70B / Llama 4 Scout | Larger alternatives if the college already serves them. | Greater weight memory and custom license terms; active MoE parameters are not total stored weights. No handwriting-grading evidence justifies choosing them first. [Llama 3.3](https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct), [Scout](https://huggingface.co/meta-llama/Llama-4-Scout-17B-16E-Instruct). |

Trillion-parameter and very large MoE models are outside the initial deployment target even if their weights are available. A claim of a small active parameter count does not make their full weights fit in laptop VRAM.

Use one primary grader initially. Introduce a second independently prompted grader for disputed/borderline criteria and an audit sample, and measure whether it improves human agreement. An ensemble is not automatically more accurate, and averaging two incompatible marks is not adjudication.

## 8. Grading With and Without a Reference Answer

### Supplied reference or rubric

Store the actual question, allowed answer types, maximum marks, criterion weights, acceptable alternatives, partial-credit rules, and any explicit penalties. A reference answer is an example of a correct response, not necessarily the only valid wording.

Grade each answer against the same version. Ask for criterion decisions, short evidence quotations from the accepted transcript, and missing or contradicted concepts. Sum criterion marks in Python. Validate bounds, finite values, allowed increments, and criterion identifiers. Do not let model arithmetic determine the total.

### No supplied reference

The model can technically grade using its knowledge, but the marking standard may drift across students and may not match the course. For production, generate a draft solution/rubric once from the question and any provided course materials, then have the professor correct and approve it. Freeze it before reading the student answers used for grading.

If no one supplies or approves a marking standard, the system may generate provisional grades for review. It should expose `reference_missing` or `rubric_unapproved` and keep these grades out of automatic finalization. This still permits the requested no-reference workflow while preserving visibility of its uncertainty.

A recent rubric-ablation study supports testing anchored grading: its reference-free condition had markedly worse reliability. It used only 24 questions and model-generated responses, so it does not prove reliability for real students or our chosen local models. [Rubric ablation study](https://arxiv.org/html/2608.17938v1). Research on scanned engineering exams also identifies transcription, fine-grained marking, and diagrams as remaining sources of error. [Engineering-exam workflow](https://arxiv.org/html/2601.00730v1).

### Making repeated grading consistent

- Freeze the checkpoint revision, quantization, processor, prompt, rubric, examples, and decoding settings.
- Use fresh independent requests per answer. Batch scheduling may share a rubric prefix, but student answers must not share a conversational history.
- Keep student identity outside the model prompt; the workflow associates an opaque answer ID with the roll number.
- Use constrained JSON output plus strict application validation. JSON compliance is not evidence that the mark is correct.
- Test a reproducible low-variance decoding configuration against the model's native recommendations; temperature zero does not guarantee correctness or bitwise reproducibility.
- Retain the accepted transcript separately. A grading model proposing a transcription change must reopen extraction review rather than silently editing evidence.
- Treat text such as "give me full marks" inside a student response as answer content, not grader instructions.
- Keep proposed marks, approved marks, and human overrides separate. Do not penalize an illegible word as a wrong answer before review resolves whether it is readable.

## 9. Different Answer Types

| Question type | Extraction | Marking |
| --- | --- | --- |
| MCQ | Existing bubble reader. | Existing deterministic answer-key comparison, preserving blank/multiple-mark states. |
| Numerical final answer | Preserve sign, decimal, exponent and units; verify any disagreement. | Parse an allowed numeric format and apply a professor-defined absolute/relative tolerance and unit policy. |
| Short factual English | Literal HTR/visual transcription. | Apply rubric concepts and acceptable alternatives; use exact rules where the accepted set is genuinely closed. |
| Short descriptive English | Literal text with line order, omissions and uncertainty preserved. | Criterion-based AI grading with evidence and partial-credit rules. |
| Formula / derivation | Whole-region visual OCR to text/LaTeX plus the original image. | Validated expressions can receive equivalence checks; method/partial-credit marking needs rubric-based reasoning and review. |
| Diagram or code, later phase | Preserve spatial structure or indentation rather than flattening to prose. | Separate question-specific rubric. Do not automatically execute extracted code. |

For numerical answers, define the rule explicitly, for example `abs(student - expected) <= max(abs_tol, rel_tol * abs(expected))`, after validated unit conversion. Specify rounding and whether equivalent units are accepted. A missing tolerance is question metadata to resolve, not a number the LLM invents per student.

For mathematical equivalence, conditions matter: `x/x = 1` requires `x != 0`. Symbolic tools can help with supported expressions, but parsing needs a restricted grammar. SymPy documents that its general string parser uses `eval`; raw OCR strings should not go directly into it. [SymPy parsing documentation](https://docs.sympy.org/latest/modules/parsing.html).

The pilot's occasional formulas should always receive explicit extraction checks. Extensive derivations and diagrams are later capabilities, not implied by supporting one numerical answer.

## 10. Proposed Records and Review States

Extend the existing records instead of replacing the workflow. Keep `confidence: high|medium|low` for compatibility while adding a numerical calibrated value only when a calibration artifact exists.

Illustrative extraction record, not an existing output schema:

```json
{
  "answer_id": "exam-question-anonymous-id",
  "q_no": 11,
  "source_page": 1,
  "crop_path": "written/Q11.png",
  "raw_text": "A process is a program in execution.",
  "accepted_text": null,
  "content_state": "present",
  "extraction_status": "needs_review",
  "model_id": "microsoft/trocr-base-handwritten",
  "model_revision": "record-the-exact-revision",
  "calibrated_correctness": null,
  "alternatives": [],
  "review_flags": [],
  "transcript_version": 1
}
```

Keep raw scores and line/region provenance alongside this record. `accepted_text` becomes populated after the acceptance gate or a human correction. An empty transcript with visible ink must stay distinguishable from a confirmed blank.

Illustrative grading record:

```json
{
  "q_no": 11,
  "transcript_version": 2,
  "rubric_version": "v1-approved",
  "marks_awarded": 1.5,
  "max_marks": 2.0,
  "confidence": "medium",
  "calibrated_grade_agreement": null,
  "reason": "Core definition present; the required distinction is missing.",
  "criteria": [
    {"id": "definition", "awarded": 1.5, "maximum": 1.5},
    {"id": "distinction", "awarded": 0.0, "maximum": 0.5}
  ],
  "needs_human_review": true,
  "status": "proposed"
}
```

Example numbers are illustrative, not model outputs. Actual criterion records also need evidence and flags. Define numerical grading confidence as agreement within a specified mark tolerance, not vague "confidence in reasoning."

Separate these three decisions: **page ownership verified**, **transcription accepted**, and **grade finalized**. The current page-verification workflow establishes the first; it does not automatically establish the other two. Corrections to ownership, crops, transcripts, or rubric versions must invalidate dependent grades and require regeneration.

## 11. Local Hardware and Runtime Plan

The user's subsequent hardware output identifies an **RTX 4050 Laptop GPU with approximately 6 GB dedicated VRAM and 16 GB system RAM**. Do not size this pilot as the originally assumed RTX 4060/8 GB machine. Shared system memory is not additional dedicated GPU memory. A friend's test computer needs its own hardware check; do not assume it matches this laptop.

The active workspace machine currently reports a Ryzen 3 7320U, Radeon 610M, and about 5.8 GiB usable physical RAM. This is separate from the user's other RTX laptop. It is appropriate for code, reports, and lightweight checks; use the other machine for the comparative GPU runs.

### User-reported setup verification, 2026-09-07

The user relayed the following results from the other laptop. Its scripts and
artifacts were not inspected or executed in this workspace:

- Python 3.11.16, PyTorch 2.11.0+cu128, torchvision 0.26.0+cu128, Transformers 4.57.6.
- `pip check`, TrOCR imports, CUDA tensor computation, and synchronization passed.
- `TrOCRWrittenOcr(device="cuda")` loaded `microsoft/trocr-base-handwritten`;
  all 333,921,792 parameters were on `cuda:0`.
- Cold loading including download took 141.526 seconds. Peak allocated GPU
  memory during loading was 1,284.61 MiB, not a measured inference peak.
- Two existing fake-backend OCR tests passed. No handwritten input was present,
  so there was no real recognition, warmed-up inference timing, or accuracy result.
- Diagnostic scripts were written under ignored `data/htr_pilot/runs/`; the
  reported Git checkout remained clean. Those files and cached weights do not
  transfer to another tester through GitHub.

This establishes reported model-loading feasibility only. Full-sheet testing
does not depend on transferring those scripts: use the existing batch CLI and
the matching manifest, as documented in the full-sheet pilot guide.

### Planning resource estimates

These are **capacity estimates for short inputs and small batches**, not measured minimums or throughput results. GPU memory depends on input pixels, output length, attention implementation, dtype, and simultaneous model loading. RAM includes room for the host application but actual loading peaks vary. File sizes exclude Python/CUDA packages and duplicated cache formats.

| Exact model/component | Parameter / download scale | Planning VRAM | Useful system RAM | CPU feasibility |
| --- | --- | --- | --- | --- |
| `microsoft/trocr-base-handwritten` | 334M; about 1.3 GB FP32 checkpoint | About 2-4 GB at reduced precision and small batch | 8 GB minimum useful, 16 GB preferred | Possible; slow comparison runs, no laptop batch SLA. |
| `microsoft/trocr-large-handwritten` | 558M; about 2.2 GB FP32 checkpoint | About 3-6 GB | 16 GB preferred | Possible but not preferred for exam throughput. |
| `PaddlePaddle/PP-OCRv6_medium_rec` | Current recognition artifact about 77 MB; detector separate | CPU possible; reserve 1-2 GB for a GPU pipeline | 4-8 GB for isolated process | Best low-resource control in this shortlist. |
| `PaddlePaddle/PaddleOCR-VL-1.6` | Advertised 0.9B class; repository about 1.93 GB | About 3-6 GB for bounded crops | 16 GB preferred | Feasible experimentally; exact backend/latency must be tested. |
| `zai-org/GLM-OCR` | Advertised 0.9B class; repository about 2.66 GB | About 3-6 GB | 16 GB preferred | Feasible through supported CPU runtime, slower than GPU. |
| `Qwen/Qwen3-VL-8B-Instruct` | About 9B including components; roughly 18 GB BF16 or 5-7 GB quantized | 8 GB is tight at 4-bit with bounded vision; 12-16 GB preferred | 16 GB useful, 32 GB preferred | Technical possibility, poor first choice for bulk latency. |
| `Qwen/Qwen3.5-9B` | Approximately 18-20 GB BF16; roughly 5-7 GB quantized | 8 GB tight at 4-bit for text-only short contexts; 12 GB preferable | 16 GB useful, 32 GB preferred | Suitable for occasional tests on a sufficiently large-RAM CPU machine, not assumed fast. |
| `Qwen/Qwen3.8-27B` | About 55.6 GB original; roughly 16-19 GB at 4-bit | Around 20-24 GB quantized; 48 GB at 8-bit; 80 GB comfortable for BF16 | 32-64 GB quantized; 96-128 GB for larger loading paths | Possible with large RAM, but not the intended production configuration. |
| `google/gemma-4-31B-it` | About 62.6 GB original; roughly 18-22 GB at 4-bit | 24 GB can be tight; 32-48 GB quantized preferred; 80 GB for BF16 | 64 GB quantized preferred; 96-128 GB for BF16 loading | Server-side experiment rather than laptop default. |

Published artifact-size references: [TrOCR-base files](https://huggingface.co/microsoft/trocr-base-handwritten/tree/main), [TrOCR-large files](https://huggingface.co/microsoft/trocr-large-handwritten/tree/main), [PP-OCRv6 recognizer files](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_rec/tree/main), [PaddleOCR-VL files](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6/tree/main), [GLM-OCR files](https://huggingface.co/zai-org/GLM-OCR/tree/main), [Qwen3.8 files](https://huggingface.co/Qwen/Qwen3.8-27B/tree/main), [Gemma4 files](https://huggingface.co/google/gemma-4-31B-it/tree/main). Quantized sizes and memory ranges above are estimates, not claimed exact distributions.

### Deployment profiles

- **Current low-RAM machine:** development, scan processing in bounded chunks, reports, PP-OCR CPU experiments. Do not attempt to keep an 8B/9B visual or grading model resident.
- **Actual RTX 4050 laptop, 6 GB VRAM / 16 GB RAM:** one extraction model at a time and bounded inputs. TrOCR-base loading has been reported successful; inference performance and accuracy are still unmeasured. Defer 8B/9B visual/grading models to larger hardware rather than promising they fit this machine.
- **Hypothetical 8 GB laptop:** quantized small-model grading remains an experiment, not the actual pilot configuration. Unload extraction models first, bound context, and measure memory and accuracy before relying on it.
- **12-16 GB GPU:** more comfortable small-model vision and grading, still schedule model residency deliberately. Useful for growing the benchmark.
- **24 GB college GPU:** compact extraction plus a quantized 27B grader in separate phases; measure remaining room before enabling simultaneous serving. Useful initial production candidate.
- **48-80 GB college GPU:** higher-precision 27B/31B comparisons and more concurrency. An 80 GB-class device is a comfortable single-GPU BF16 evaluation target; two independently resident large graders may require separate GPUs.

A 4 GB GPU is a useful starting point for small-batch base HTR inference; 8 GB is a better development minimum for comparing the compact extraction models. This does not imply that full fine-tuning fits the same memory budget.

### Packages, quantization, and installation

| Component | Initial integration | Alternative / deployment note |
| --- | --- | --- |
| Existing crop/alignment | NumPy, Pillow, OpenCV, manifest helpers already in repo | Keep these modules and outputs. |
| TrOCR | PyTorch + Transformers; existing `[htr]` extra | Use FP16/BF16 on supported GPU; establish accuracy before INT8/INT4 or ONNX export. |
| PaddleOCR-VL | Element recognition through Transformers; its current example requires Transformers 5+ | Full `paddleocr[doc-parser]` pipeline is optional. Isolate dependencies until compatibility with existing TrOCR is tested. |
| PP-OCRv6 | PaddleOCR recognition module and appropriate Paddle runtime | Official ONNX artifacts exist; ONNX Runtime can be considered for CPU deployment. |
| GLM-OCR | Transformers or a supported local Ollama runtime | Native text/formula prompt formats matter. |
| Local grader | Ollama for laptop simplicity, or a local HTTP adapter to a pinned model server | Use schema-constrained generation and Pydantic validation; pin the actual model digest, not only a floating tag. |
| College batching | Linux + vLLM, if chosen checkpoint and quantization are supported by the pinned release | WSL2 for Windows experiments; native Windows is not an official vLLM target. |
| Evaluation | `jiwer` for CER/WER; scikit-learn/SciPy for calibration/agreement as needed | Add only to evaluation dependencies, not mandatory generator dependencies. |
| Numerical / symbolic | Decimal and restricted numeric parsing; optional SymPy/Pint later | Add a bounded expression parser and explicit units/tolerance metadata first. |

The current PaddleOCR-VL card supplies a direct Transformers path for region recognition, which fits our crops. vLLM and Ollama document schema-constrained output, but validation of mark semantics remains our responsibility. [Paddle integration](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6), [vLLM structured output](https://docs.vllm.ai/en/latest/features/structured_outputs/), [Ollama structured output](https://docs.ollama.com/capabilities/structured-outputs), [vLLM platform requirements](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/).

Use llama.cpp/GGUF only for explicitly supported architectures and the correct visual projector, not as a universal converter. The project documents OCR-specific prompt requirements. ONNX and TensorRT are optimization work after correctness and model selection, especially for autoregressive image models. [llama.cpp multimodal guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md).

All recommended primary local models have downloadable weights and can work offline after dependencies and checkpoints are cached. There is no per-answer API fee; hardware, electricity, storage, maintenance, and human review still cost resources. For Surya/Chandra, free research access should not be conflated with unrestricted operational licensing; inspect the exact weight terms for the planned deployment.

## 12. Latency and Batch Scale

There is no defensible measured seconds-per-answer figure yet for our models on the user's RTX laptop. Token count, power limits, image dimensions, precision, beams, context, and batching can change the result substantially. Neither parameter count nor a vendor pages-per-second result determines SmartOMR latency.

Expected ordering to test: PP-OCR is the low-compute baseline; dedicated/compact extraction should usually cost less than an 8B+ visual read; grading time grows with generated reasoning and structured-output length. This is a planning expectation, not a benchmark result. Measure cold loading separately from warm inference, and include preprocessing, queue time, retries, and disk writes in end-to-end measurements.

For **300 students with 10 written questions each**, there are **3,000 answer jobs**, regardless of how many PDF pages contain them. With two written lines per answer there may be about 6,000 HTR line jobs, but the actual count must come from the writing.

For a simple sequential capacity estimate:

```text
T = T_scan + N_answers * (t_primary + fallback_fraction * t_fallback + t_grade)
    + T_model_loads + T_retries
```

Illustrative sensitivity calculation only, not model predictions:

| Assumed warm time per answer | Fallback fraction | 3,000-answer compute time |
| --- | --- | --- |
| Primary 0.5 s, fallback 2 s, grading 2 s | 20% | 145 minutes, before scan/loading/retries |
| Primary 1 s, fallback 5 s, grading 8 s | 20% | 500 minutes, before scan/loading/retries |

Batched throughput is a different measurement from individual latency. Record p50/p95 answer latency and completed answers/second with a specified concurrency. Do not divide the sequential estimate by a guessed GPU speedup. The benchmark should report these for each model/precision on the same machine.

Keep the current file-based workflow initially. Add resumable jobs keyed by crop hash, model configuration, and question identity. Save successful answers incrementally and retry only failures. Separate GPU extraction/grading from scan alignment so changing a model does not require re-parsing every scanned page. Load models once per worker and process bounded batches instead of creating one model instance per answer.

## 13. Objective Evaluation Before Selecting Winners

### Dataset

Start with **300-500 real answer crops from at least 30-50 writers**, representing the actual examination sheets. This is a proposed initial selection budget, not enough to certify a very low production error rate.

Include prose, numbers, occasional formulas, blanks, crossed-out material, faint ink, neat and messy writers, scanned pages and phone photos. Include deliberately wrong answers, misspellings, unusual abbreviations, and meaningful pairs such as "is"/"is not". Do not populate the entire set with correct textbook sentences.

Record image/crop ID, anonymous writer ID, source-sheet ID, question ID, question text, reference/rubric version, literal human transcription, content type, quality tags, and human criterion marks. Two transcribers/graders should independently label a substantial subset, with disagreement adjudicated. An illegible span should be labeled uncertain, not forced into fabricated ground truth.

Use a development/calibration/test split by writer, for example 40/30/30 with enough writers per split. Keep duplicate photographs and augmented versions of the same physical answer in one split. Add a held-out-question or later-exam test to measure grading transfer beyond familiar questions. Never tune prompts or thresholds on the locked test set.

### Extraction metrics

- Character Error Rate and Word Error Rate, aggregated over the dataset rather than only averaging percentages of unequal-length answers.
- Exact full-answer match and the proportion of answers with a critical sign/number/negation error.
- Blank false-positive/false-negative rates and unsupported text emitted on truly blank crops. Report these separately from CER/WER denominators.
- Line omission, duplication, wrong order, edge truncation, and unexplained ink rates.
- Accepted-set error versus coverage, fallback rate, and review burden by writer and capture condition.
- Latency, throughput, peak RAM/VRAM, retries, and truncation/error rates.

Use a literal track preserving case, punctuation, spelling, and relevant structure. Also report a separately specified whitespace/typography-normalized track. Do not normalize away signs, units, decimals, negation, or mathematical distinctions. `jiwer` provides edit-based metrics and defined behavior for empty references; agree on that policy explicitly. [Jiwer documentation](https://jitsi.github.io/jiwer/).

For formulas, include symbol/operator correctness, structural correctness, and appropriate rendered/equivalence checks. Literal LaTeX-string differences alone can overcount errors between equivalent encodings, while text edit distance can underweight a wrong exponent.

### Grading metrics and experiments

Evaluate the grader twice: once using **human transcriptions**, and once using **automatic extractions**. The difference estimates the downstream effect of OCR mistakes. Also compare direct image grading on the same samples as an experimental baseline, especially for formulas; do not silently mix it into the text-only results.

Report per-question and normalized mean absolute error, signed bias, exact mark agreement, agreement within the professor's tolerance, quadratic weighted kappa for discrete scales, and material over/under-marking rates. Examine criterion agreement and confidence intervals. A high score correlation alone can hide systematic over-marking.

Test repeated grading, reordered requests, equivalent answer wording, longer irrelevant text, incorrect negation, grading instructions embedded in answers, blank answers, wrong but fluent answers, and rubric changes. Compare the fixed-reference and no-reference provisional modes. Measure model disagreement against adjudicated human marks rather than assuming consensus is truth.

Compare four extraction conditions before adding complexity: raw aligned crop, mild enhancement, current preprocessing, and corrected line segmentation. Compare full-precision and deployed quantized variants on identical answers. Choose the smallest model meeting the quality target, using paired comparisons and writer-level uncertainty estimates.

### Acceptance criteria

Define the tolerated grade error and allowable review rate with the professor. An illustrative extraction goal is at least 99% exact correctness among accepted short answers, with critical-token failures separately controlled; it is a proposed target, not an achieved claim or a preset threshold.

With zero observed errors in 300 independent accepted examples, the rough one-sided 95% upper error bound is still about 1% (`3/n`). Writer correlation reduces effective independence. Therefore 300-500 pilot crops cannot certify 99.9% reliability. Expand evaluation before making deployment-level accuracy claims, and retain a randomly reviewed sample of apparently confident outputs.

There is currently **no established SmartOMR handwriting accuracy or AI grading agreement percentage**. The 15 passing software tests and published external benchmarks cannot substitute for this dataset.

## 14. Fine-Tuning and Data Collection

Collecting our own labeled data is valuable immediately for model selection, error diagnosis, and calibration. It may later improve the recognizer through fine-tuning, particularly for local abbreviations, engineering terminology, pen styles, and capture conditions.

Do not fine-tune on the locked test set. Grow a separate training collection, initially perhaps 1,000-3,000 real labeled lines across many writers, then measure a learning curve. The gain depends more on diversity and correct labels than on reaching a specific count. Include realistic geometric and lighting augmentation without inventing new characters or deleting strokes.

Compare full fine-tuning, partial encoder freezing, and adapter methods on the college GPU if adaptation is needed. Full training includes gradients and optimizer state, so inference VRAM estimates do not apply. Use validation to choose the method; neither full tuning nor LoRA is automatically best.

Grader fine-tuning comes later. First establish a sound rubric and human agreement, then investigate rubric-based examples or adaptation on adjudicated criterion labels. Do not train the grader solely on its own unreviewed marks. Collect failure cases from reviewers while monitoring whether the dataset becomes biased toward only difficult answers.

## 15. Cases That Require Review

- Unverified student/page ownership, missing answer pages, or an alignment failure affecting the answer region.
- Visible ink with empty extracted text; uncertain blank detection; faint or clipped writing.
- Unexpected line structure, possible overflow, or answer content split between regions.
- Truncated/repetitive output, unsupported text, or contradictory model readings.
- Disagreement in a numeral, operator, unit, negation, exponent, or meaning-changing technical term.
- Mathematical structure or diagrams outside the validated pilot scope.
- Missing question text, unapproved rubric, or a reference-free provisional grade.
- Invalid schema, nonfinite/out-of-range marks, unsupported criterion evidence, or inconsistent criterion totals.
- Large grading disagreement, borderline scores under the professor's policy, or an alternative valid solution not covered by the rubric.

Retain image, transcript candidates, rubric, criterion reasons, and a correction history together. Identity verification and grade release should remain separate actions in the existing review workflow.

## 16. Integration Without Restructuring the Project

Keep the existing package ownership: `reader` for extraction, `grading` for scoring, `workflows` for orchestration, and `io`/models for validated records. No repository-wide redesign is needed.

Extend the line-only backend with a small answer-level adapter contract that can return regions/lines and whole-answer candidates. Reuse the current TrOCR backend inside it. Keep backward-compatible serialization so previously parsed students and manual grading packets remain readable.

Move optional inference into an independently rerunnable written-extraction step over saved, verified crops. Preserve the current parse-time OCR option for compatibility. Add a few batch operations, caching, and resumability when supported by the selected backend; do not install every researched model into the base environment.

Pass accepted-transcript status/version and extraction flags into `WrittenGradeRequest`. Extend result validation and storage for criterion scores, provenance, and strict finite numbers. Keep the mock provider explicitly a testing provider. Add the local model adapter behind `WrittenGrader`; the workflow should remain unaware of whether the inference process runs on the laptop or a college server.

Extend question metadata with answer type, approved rubric version, criterion weights, acceptable answers, and optional numerical rules. Keep scan geometry in the manifest and grading semantics in question/rubric metadata. Link them by exam/question IDs.

Extend the written review page to show raw image, accepted transcription, uncertain regions, criterion marks, and separate extraction/grading states. Record edits and invalidate dependent grades when an accepted transcript changes. File-based artifacts are sufficient for the first pilot; database/queue changes should follow measured concurrency and operational requirements.

## 17. End-to-End Architecture and Next Work

```text
Scanned exam PDF / phone images + matching manifest
  -> existing alignment and page-quality checks
  -> existing student grouping and ownership verification
  -> manifest answer crop + preserved color/context evidence
  -> raw-ink presence, edge/overflow, quality and content-type checks
  -> prose: detected lines -> TrOCR-base
     formulas/mixed regions: whole crop -> PaddleOCR-VL-1.6
  -> calibrated extraction gate
     uncertain prose -> independent PaddleOCR-VL-1.6 / selected visual reader
     unresolved disagreement -> human transcription review
  -> versioned accepted answer text / formula structure + original image
  -> question-type routing
     MCQ -> deterministic key
     numerical -> validated tolerance/unit rules
     prose -> benchmark-selected grader on adequately provisioned college hardware
     formulas -> supported symbolic checks + rubric-based visual/text review
  -> fixed approved question rubric + criterion evidence
  -> deterministic validation and mark summation
  -> proposed marks; human review when required
  -> approved grades + existing final score exports
```

These are selected pilot components, not a claim that all are implemented or empirically optimal. The benchmark can replace the primary extractor or grader while preserving the architecture.

The next implementation should proceed in this order:

1. Create a versioned evaluation dataset contract and reproducible benchmark harness, with writer-disjoint splits and empty-answer handling.
2. Fix raw crop preservation, unsafe rule removal, line coverage, actual-token confidence, and nonfinite mark validation; add focused regression tests for those behaviors.
3. Add the whole-answer OCR adapter and compare the shortlisted readers on labeled real answers, including quantization and preprocessing variants.
4. Calibrate acceptance/review routing and implement an editable, versioned transcription review record.
5. Add the local rubric-based grader with strict criterion validation, and test it separately on human transcriptions before running the end-to-end evaluation.
6. Run a fully reviewed college pilot and measure accepted-answer errors, grading agreement, review workload, and complete-exam throughput before increasing automation.

This investigation produced the decision document and read-only diagnostic results. It did not install model weights in this workspace, send student images to external inference services, modify the parser/grader, or produce new student marks. The later, user-reported model download/loading on the other laptop is recorded separately in Section 11.
