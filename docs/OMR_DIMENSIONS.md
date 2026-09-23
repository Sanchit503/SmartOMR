# Current OMR Generator Dimensions

Checked against the local generator on 2026-09-21. This is a measurement
reference for the vertical numerical layout and 3.5 mm bubbles, not a
print-quality certification. New sheets use manifest schema v6.

Sources: [metrics.py](../omr/generator/metrics.py),
[layout.py](../omr/generator/layout.py), [flow.py](../omr/generator/flow.py),
[pdf_gen.py](../omr/generator/pdf_gen.py), and
[geometry.py](../omr/contracts/geometry.py).

## Units And Meaning

- Geometry is in millimetres at actual-size / 100% printing. Printer scaling
  changes the physical measurements. Confirm the target printer does not clip.
- Coordinates start at the page's top-left: x increases right, y increases down.
- Marker and bubble coordinates are centres. Box coordinates are top-left edges.
- Text y positions are baselines unless a band height is explicitly stated.
- Pitch means centre-to-centre spacing, or box-start-to-box-start spacing.
- Box dimensions and circle diameters describe drawing paths. Outlined shapes
  have ink extending half a line thickness on each side of that path.
- Font sizes and stroke widths are PDF points: 1 pt = 25.4/72 mm, about 0.35278 mm.
  Font point size is not the measured height of every visible letter.
- These are current defaults. An older printed exam must be checked against its
  own PDF and manifest, not regenerated coordinates from newer code.

## Paper And Content Frames

| Item | Measurement |
| --- | --- |
| Paper | A4 portrait, 210 x 297 mm |
| Design printer-safe margin | 10 mm on each edge |
| Printer-safe rectangle | x=10..200, y=10..287; 190 x 277 mm |
| Main content left/right | x=12 and x=198 mm |
| Main content width | 186 mm |
| Lowest allowed question-layout coordinate | y=274.5 mm |
| Page 1 identity frame | x=12, y=42.5; 186 x 76.75 mm; bottom y=119.25 |
| Page 2+ identity frame | x=12, y=31; 186 x 19 mm; bottom y=50 |
| Identity-to-question-band gap | 5 mm |
| Page 1 question band | y=124.25..274.5; 150.25 mm high |
| Page 2+ question band | y=55..274.5; 219.5 mm high |

The question band includes section headings and spacing. It is not all writable
answer space. A continuation page has 69.25 mm more vertical question-layout room.

## Registration Marks

| Item | Size / placement |
| --- | --- |
| Four corner squares | Each 7 x 7 mm, filled black, no outline stroke |
| Square outer-edge distance from nearest page edges | 12 mm |
| Top-left centre | (15.5, 15.5) |
| Top-right centre | (194.5, 15.5) |
| Bottom-left centre | (15.5, 281.5) |
| Bottom-right centre | (194.5, 281.5) |
| Quiet space around each square | 3.5 mm beyond its edges |
| Reserved region around each square | 14 x 14 mm |
| Orientation square | 3.5 x 3.5 mm, centre (29.5, 15.5) |
| Orientation reserved region | 10.5 x 10.5 mm |
| Each page-index bar | 4 x 1.8 mm |
| Bar centre spacing | 6 mm; nominal edge gap 2 mm |
| Bar centre y | 15.5 mm |
| Rightmost bar centre x | 180.5 mm |
| N-bar strip width | 4 + 6(N-1) mm |

Corner reserved rectangles are `(8.5,8.5)-(22.5,22.5)`,
`(187.5,8.5)-(201.5,22.5)`, `(8.5,274.5)-(22.5,288.5)`, and
`(187.5,274.5)-(201.5,288.5)`. Quiet space may extend outside the design safe
rectangle because it contains no ink. The orientation keep-out is
`(24.25,10.25)-(34.75,20.75)`.

For a two-page exam, bar centres are (174.5,15.5) and (180.5,15.5).
For N pages, bar i is at `x=180.5-6(N-i)`, where i starts at 1.
The current page's bar is filled; others are outlined. These encode page number,
not student identity. The current generator has no unique booklet QR code.

## Common Bubbles

All program, roll-digit, MCQ, and numerical bubbles use the same geometry:

| Property | Measurement |
| --- | --- |
| Drawn radius | 1.75 mm |
| Nominal diameter | 3.5 mm |
| Outline stroke | 1 pt, about 0.35278 mm |
| Actual outside ink diameter, before rasterization/printing effects | About 3.85278 mm |
| Published sampling radius in manifest | 1.26 mm (72% of drawn radius) |
| Sampling diameter | 2.52 mm; this circle is not printed |

Nominal circle gaps below use the 3.5 mm drawing-path diameter. The physical white
gap between printed outlines is about 0.35278 mm smaller.

## Page 1 Identity

Program selector centres are BTECH=(28,47.5), MTECH=(128,47.5),
PHD=(153,47.5), all with radius 1.75 mm.

| Property | BTech | Shared MTech/PhD |
| --- | --- | --- |
| Numeric positions | 7 | 5 |
| Bubble rows per position | 10, digits 0..9 | 10, digits 0..9 |
| First column centre x | 28 mm | 128 mm |
| Last column centre x | 88 mm | 168 mm |
| First/last digit-row centre y | 62 / 116 mm | 62 / 116 mm |
| Column pitch | 10 mm | 10 mm |
| Nominal horizontal bubble gap | 6.5 mm | 6.5 mm |
| Row pitch | 6 mm | 6 mm |
| Nominal vertical bubble gap | 2.5 mm | 2.5 mm |
| Bubble-grid footprint, excluding labels | 63.5 x 57.5 mm | 43.5 x 57.5 mm |
| Handwriting cell size | 7 x 7 mm | 7 x 7 mm |
| Handwriting field top-left | (24.5,51) | (124.5,51) |
| Complete handwriting field size | 67 x 7 mm | 47 x 7 mm |
| Handwriting cell pitch | 10 mm | 10 mm |
| Nominal gap between handwriting cells | 3 mm | 3 mm |

Both handwriting fields end at y=58 mm. The digit-0 bubble paths start at
y=60.25 mm, leaving a nominal 2.25 mm between box bottom and bubble top.
Digit labels are centred 7.5 mm left of the first bubble column. The last
bubble paths end at y=117.75 mm; the identity border is 1.5 mm below them.

MTech and PhD have separate program selectors but share the five-position
write-in field and numeric bubble grid. The chosen program supplies the prefix.

## Continuation-Page Identity

There is no roll-digit bubble grid on pages 2+. Program-selector centres are
BTECH=(20,35), MTECH=(134,35), and PHD=(159,35).

| Property | BTech | Shared MTech/PhD |
| --- | --- | --- |
| Handwriting cells | 7 | 5 |
| Each cell | 7 x 7 mm | 7 x 7 mm |
| Field top-left | (18,39.5) | (132,39.5) |
| Complete field size | 79 x 7 mm | 55 x 7 mm |
| Cell pitch | 12 mm | 12 mm |
| Nominal gap between cells | 5 mm | 5 mm |
| Field bottom y | 46.5 mm | 46.5 mm |

Thus the handwriting boxes themselves are the same size on all pages; their
spacing is wider on continuation pages.

## MCQ Section

| Item | Measurement |
| --- | --- |
| Option bubble diameter | 3.5 mm |
| Option centre pitch | 8 mm; nominal gap 4.5 mm |
| Question-row centre pitch | 8 mm; nominal gap 4.5 mm |
| Q-label origin to first option centre | 12 mm horizontally |
| Section-header allocation | 6 mm |
| Option-letter header offset | 4.5 mm above first bubble-row centre |
| First bubble-row centre | Section-band top + 10.5 mm |
| Candidate column counts | 2, 3, 4; 1-column fallback exists |
| Column allocations for 2 / 3 / 4 columns | 93 / 62 / 46.5 mm |
| Supported options per question | 2..6 |

For k options, minimum allowed column width is `12 + 8(k-1) + 10` mm.
Actual bubble-only width is `3.5 + 8(k-1)` mm; four options occupy 27.5 mm.
Column count minimizes total exam pages; ties prefer fewer columns. Therefore
the column count and question coordinates are not fixed for every exam.

For column c starting at zero, its left edge is `12 + c*(186/columns)` mm.
Option j has centre `column_left + 12 + 8*j`. In a two-column layout the first
option centres are x=24 and x=117 mm.

If MCQs begin at the top of the question band, the first row is y=134.75 on page 1
or y=65.5 on continuation pages. Capacity is 18 rows per column on page 1 and
26 on later pages when the available space is filled with MCQs.

## Vertical Numerical Section

| Item | Measurement |
| --- | --- |
| Questions across, for 1..3 digit answers | 4 |
| Minimum allocation width | 42 mm |
| Gap between allocations | 6 mm |
| Allocation left edges for four equal 42 mm slots | x=12, 60, 108, 156 mm |
| First digit-bubble centre offset, 2-digit grid | 16.5 mm; grid centred in slot |
| First digit-bubble centre x, four 2-digit questions | 28.5, 76.5, 124.5, 172.5 mm |
| Digit options down each column | 0..9, ten bubbles |
| Digit-row centre pitch | 5.2 mm; nominal gap 1.7 mm |
| Bubble-only column height | 50.3 mm |
| Digit-column centre pitch | 9 mm; nominal gap 5.5 mm |
| First bubble-row centre below question allocation top | 8 mm |
| Digit-label right anchor | 5 mm left of first bubble centre |
| Place-value labels | Not printed; only the 0-9 row labels remain |
| Q-label baseline | Question allocation top + 2 mm |
| Q-label horizontal anchor | 9 mm left of the first bubble column, for every digit count |
| Question-row allocation gap | 5 mm |
| Section-heading/instruction allocation | 12 mm |
| Supported digit positions | 1..8 per question |

There are no digit handwriting boxes or place-value headings in numerical
questions. Columns still run left to right from the most significant digit to
the least significant digit. Students fill every column, including leading zeros.
Question labels follow the grid's position, so the heading-to-grid offset stays
constant even when a narrow grid is centred within a wider allocation.

| Positions | Bubble-only footprint | Full question allocation | Questions per row, for equal-width questions |
| --- | --- | --- | --- |
| 1 | 3.5 x 50.3 mm | 42 x 58.55 mm | 4 |
| 2 | 12.5 x 50.3 mm | 42 x 58.55 mm | 4 |
| 3 | 21.5 x 50.3 mm | 42 x 58.55 mm | 4 |
| 4 | 30.5 x 50.3 mm | 45 x 58.55 mm | 3 |
| 5 | 39.5 x 50.3 mm | 54 x 58.55 mm | 3 |
| 6 | 48.5 x 50.3 mm | 63 x 58.55 mm | 2 |
| 7 | 57.5 x 50.3 mm | 72 x 58.55 mm | 2 |
| 8 | 66.5 x 50.3 mm | 81 x 58.55 mm | 2 |

Allocation width is `max(42, 9*digits + 9)` mm; height is always 58.55 mm.
The next row starts 63.55 mm later, including its 5 mm gap. Variable widths are
packed in question order without splitting a grid across pages. The 12 mm
section header is additional and repeated after page breaks. These allocations
are layout regions, not printed rectangular frames.

For numerical-only exams with two-digit answers, eight questions fit on page 1
and twelve on a full continuation page. MCQs and written sections reduce that
capacity. This is intentionally four across, not a maximum-density five-across
layout. Older horizontal grids remain readable using their original manifests.

## Written Answers

| Item | Measurement |
| --- | --- |
| Box left edge | x=12 mm |
| Box width | 186 mm |
| Ruled-line spacing | 7 mm |
| Box height | 7 x configured line count, in mm |
| Question-header allocation above each box | 6 mm |
| Question-label baseline | 2 mm above box top |
| Reserved gap after each box | 5 mm |
| Box-bottom to following box-top on same page | 11 mm, including following 6 mm question-header allocation |
| Section heading allocation | 6 mm, repeated after page breaks |

For 1, 2, 3, 4, 5, 6 lines, box sizes are respectively 186 x 7, 186 x 14,
186 x 21, 186 x 28, 186 x 35, and 186 x 42 mm.
Each question occupies `6 + 7*lines` mm before its trailing 5 mm gap.
A fresh question band can hold a single box of at most 19 lines on page 1 or
29 on a continuation page after reserving the section/question headers.

## Section Flow And Example

Order is MCQs, then numerical questions, then written answers. Sections use
remaining space rather than always starting a new page. Individual answer boxes
and numerical grids are not split across pages.

The MCQ-to-next-section cursor gap is 7 mm. Numerical-to-written transition
adds 7 mm after the numerical section's existing trailing 5 mm row gap. Header
allocations are additional; these constants are not all literal blank ink gaps.

A checked layout with 10 four-option MCQs plus 10 two-line written questions:

- Two pages; two MCQ columns.
- Page 1: all 10 MCQs; Q11, Q12, Q13 written-box tops y=187.5,212.5,237.5 mm.
- Page 2: Q14..Q20 written-box tops y=67,92,117,142,167,192,217 mm.
- Every written box is 186 x 14 mm.

This example is not a universal placement: question counts, option counts, and
answer sizes change the flow. Every actual question coordinate is in that
generated exam's manifest.

## Header And Typography

| Element | Font and nominal size | Baseline / placement |
| --- | --- | --- |
| University, page 1 | Helvetica-Bold 10.5 pt | Centred x=105, y=25.8 mm |
| Exam title, page 1 | Helvetica-Bold 11.5 pt | x=12, y=30 mm |
| Course/exam ID/marks, page 1 | Helvetica 9 pt | x=12, y=34 mm |
| Instructions, page 1 | Helvetica 7 pt | x=12, y=37.4 and 40.3 mm |
| Continuation header | Helvetica-Bold 10.5 pt | x=12, y=27 mm |
| Page counter | Helvetica 9.5 pt | Right aligned at x=198; y=34 on page 1, y=27 later |
| Program labels, page 1 | Helvetica-Bold 8.5 pt | x=selector centre+4.5 mm; baseline 3 pt below selector centre |
| Roll titles, page 1 | Helvetica-Bold 8 pt | x=49 / 169 mm; baseline 3 pt below selector centre |
| Program labels, later pages | Helvetica-Bold 7.8 pt | x=selector centre+4 mm; baseline 3 pt below selector centre |
| Roll titles, later pages | Helvetica-Bold 7.5 pt | x=47 / 174 mm; baseline 3 pt below selector centre |
| Roll digit labels | Helvetica 6.5 pt | 7.5 mm left of first bubble column; baseline 2.2 pt below row centre |
| Section headings | Helvetica-Bold 10 pt | Determined by section location |
| MCQ option letters | Helvetica-Bold 7 pt | Above first row; baseline 2 pt below its header reference |
| MCQ question numbers | Helvetica 8 pt | Baseline 2.5 pt below bubble-row centre |
| Numerical instructions | Helvetica 7.5 pt | In the 12 mm section-header allocation |
| Numerical question label | Helvetica-Bold 8 pt; question number only | 9 mm left of first bubble column; allocation top + 2 mm |
| Numerical digit labels | Helvetica 6.5 pt | Baseline 2.2 pt below bubble-row centre |
| Written question label | Helvetica 8 pt; question number and maximum-line guidance, no marks | Baseline 2 mm above answer box |

Written labels read `Q9 - answer in a maximum of 2 lines`, using the configured
count and singular `line` when that count is one. The box height is unchanged.

The fitted university/title/metadata/instruction strings and numerical question
labels may shrink to a floor of 5.5 pt. Other fonts are fixed. This floor is not
a guarantee that an arbitrarily long string will fit. Header text reserves the
measured page-counter width plus 6 mm when a counter is printed. The counter is
omitted on a one-page exam; page-index bars are still generated.

## Stroke Widths And Raster Dimensions

| Element | Stroke width |
| --- | --- |
| Bubble outlines | 1 pt = 0.35278 mm |
| Identity frames, handwriting cells, outlined page bars, inner writing rules | 0.7 pt = 0.24694 mm |
| Written answer outer borders | 0.9 pt = 0.31750 mm |
| Solid markers / filled page bar | Filled shapes without an outline stroke |

At 200 DPI, the shared geometry helper rounds A4 to 1654 x 2339 pixels;
at 300 DPI, to 2480 x 3508 pixels. Pixel positions use `mm * DPI / 25.4` with
rounding. The PDF is vector output, so these raster sizes are not physical
generator dimensions or a claim about a scanner's effective resolution.

## Where To Change Things

- `metrics.py`: physical sizes, spacing, margins, and derived content frames.
- `layout.py`: identities and registration marks assembled from those metrics.
- `flow.py`: question placement, column choice, and page breaking.
- `pdf_gen.py`: fonts, strokes, wording, and some text-anchor offsets.
- `manifest.py`: publication of the generated geometry for the reader.

Do not edit only a rendered PDF or change old manifests to pretend old printed
sheets have the new layout. A new layout requires a matching new PDF/manifest,
with rendered-output checks before printing a class batch.
