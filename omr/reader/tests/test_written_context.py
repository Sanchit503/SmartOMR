import numpy as np
from PIL import Image

from omr.reader.written import _context_box, _header_bottom_mm, _page_regions, crop_written_responses


def test_context_includes_outside_ink_and_stops_between_questions(tmp_path):
    first = dict(q_no=1, page=1, x_mm=10, y_mm=20, heading_top_mm=16, width_mm=40,
                 height_mm=10, max_marks=1, lines=2)
    second = dict(first, q_no=2, y_mm=40, heading_top_mm=36)
    manifest = {"written_block": [first, second], "page": {"width_mm": 80, "height_mm": 80}}
    box = _context_box(first, manifest, 0)
    assert box["y_mm"] == 0
    assert box["y_mm"] + box["height_mm"] == 34.5
    image = np.full((80, 80), 255, dtype=np.uint8)
    image[18, 15] = 0
    image[33, 15] = 100  # Nearby ink below the former midpoint boundary.
    image[36, :] = 99  # Next question's heading must remain excluded.
    crops = crop_written_responses({1: image}, manifest, tmp_path, 25.4)
    raw = np.asarray(Image.open(crops[0].crop_path))
    assert raw[18, 15] == 0
    assert raw[33, 15] == 100
    assert not np.any(raw == 99)
    assert crops[0].height_mm == 34
    assert Image.open(crops[0].page_header_path).size == (80, 16)


def test_context_scales_with_manifest_geometry():
    entry = dict(q_no=1, page=1, x_mm=10, y_mm=20, width_mm=40, height_mm=10)
    neighbour = dict(entry, q_no=2, y_mm=40)
    manifest = {"written_block": [entry, neighbour], "page": {"width_mm": 80, "height_mm": 80}}
    original = _context_box(entry, manifest)
    scaled_entry = {key: value * 2 if key.endswith("_mm") else value for key, value in entry.items()}
    scaled_neighbour = {key: value * 2 if key.endswith("_mm") else value for key, value in neighbour.items()}
    scaled = _context_box(scaled_entry, {"written_block": [scaled_entry, scaled_neighbour],
                                       "page": {"width_mm": 160, "height_mm": 160}})
    for key in ("x_mm", "y_mm", "width_mm", "height_mm"):
        assert scaled[key] == original[key] * 2


def test_numerical_neighbour_bounds_follow_manifest_orientation():
    question = dict(q_no=1, page=1, x_mm=10, y_mm=20, positions=2,
                    position_pitch_mm=6, digit_pitch_mm=4, orientation='horizontal')
    manifest = dict(numerical_block=[question], bubble_radius_mm=1.6)
    region = _page_regions(manifest, 1)[0]
    assert region['width_mm'] == 36 + 3.2
    assert region['height_mm'] == 6 + 3.2


def test_page_header_stops_before_first_question_bubbles(tmp_path):
    written = dict(q_no=2, page=1, x_mm=10, y_mm=50, width_mm=40, height_mm=10,
                   max_marks=1, lines=2)
    mcq = dict(q_no=1, page=1, x_mm=10, y_mm=30, options=['A', 'B'])
    manifest = dict(written_block=[written], mcq_block=[mcq], bubble_radius_mm=2,
                    mcq_option_pitch_mm=6, page={'width_mm': 80, 'height_mm': 80})
    crops = crop_written_responses({1: np.full((80, 80), 255, dtype=np.uint8)}, manifest, tmp_path, 25.4)
    assert Image.open(crops[0].page_header_path).height == 28


def test_header_preserves_roll_digits_but_excludes_section_heading(tmp_path):
    written = dict(q_no=11, page=1, x_mm=10, y_mm=50, width_mm=40, height_mm=10, max_marks=2, lines=2)
    manifest = dict(written_block=[written], mcq_block=[dict(q_no=1, page=1, x_mm=10, y_mm=30, options=['A','B'])],
        roll_number_block=dict(page=1, btech_digits=dict(y_mm=10, row_pitch_mm=1)),
        write_in_fields=[dict(page=1,x_mm=10,y_mm=5,width_mm=20,height_mm=4)],
        bubble_radius_mm=2, mcq_option_pitch_mm=6, page=dict(width_mm=80,height_mm=80))
    image = np.full((80,80),255,dtype=np.uint8)
    image[20,20]=0  # Lowest roll bubble, inside the identity block.
    image[26,:]=99  # Unwanted section heading, above the first question bubbles.
    image[54,20]=0  # Student answer.
    crops=crop_written_responses({1:image},manifest,tmp_path,25.4)
    header=np.asarray(Image.open(crops[0].page_header_path))
    assert _header_bottom_mm(manifest,1)==22.5
    assert header[20,20]==0
    assert not np.any(header==99)
    answer=np.asarray(Image.open(crops[0].crop_path))
    assert answer.shape[1]==80
    assert np.any(answer==0)


def test_header_boundary_scales_from_manifest_not_fixed_height():
    def manifest(scale):
        return dict(written_block=[dict(page=1,q_no=11,x_mm=10*scale,y_mm=50*scale,width_mm=40*scale,height_mm=10*scale)],
            mcq_block=[dict(page=1,q_no=1,x_mm=10*scale,y_mm=30*scale,options=['A','B'])],
            roll_number_block=dict(page=1,btech_digits=dict(y_mm=10*scale,row_pitch_mm=scale)),
            bubble_radius_mm=2*scale,mcq_option_pitch_mm=6*scale)
    assert _header_bottom_mm(manifest(2),1)==2*_header_bottom_mm(manifest(1),1)


def test_continuation_header_uses_its_own_identity_fields():
    manifest=dict(written_block=[dict(page=2,q_no=14,x_mm=10,y_mm=32,heading_top_mm=28,width_mm=40,height_mm=10)],
        write_in_fields=[dict(page=2,x_mm=10,y_mm=12,width_mm=20,height_mm=6)],
        roll_number_block=dict(page=1,btech_digits=dict(y_mm=60,row_pitch_mm=6)),
        continuation_program_choices=[dict(page=2,y_mm=10,radius_mm=2)],bubble_radius_mm=2)
    assert _header_bottom_mm(manifest,2)==21


def test_legacy_question_crop_preserves_heading_and_excludes_next_heading():
    first=dict(q_no=11,page=1,x_mm=12,y_mm=188,width_mm=186,height_mm=14,max_marks=2,lines=2)
    second=dict(first,q_no=12,y_mm=213)
    manifest=dict(written_block=[first,second],mcq_block=[dict(q_no=10,page=1,x_mm=120,y_mm=167,options=['A','B','C','D'])],
                  bubble_radius_mm=2,mcq_option_pitch_mm=8,page=dict(width_mm=210,height_mm=297))
    context=_context_box(first,manifest)
    assert context['y_mm']==175
    assert context['y_mm']+context['height_mm']==205


def test_bottom_context_handles_tight_gaps_and_page_boundary():
    first = dict(q_no=1, page=1, x_mm=10, y_mm=20, width_mm=40, height_mm=10)
    second = dict(first, q_no=2, y_mm=35, heading_top_mm=31)
    manifest = dict(written_block=[first, second], page=dict(width_mm=80, height_mm=80),
                    printer_safe_margin_mm=2)
    context = _context_box(first, manifest)
    assert context['y_mm'] + context['height_mm'] == 30.75
    last = _context_box(second, manifest)
    assert last['y_mm'] + last['height_mm'] == 78
