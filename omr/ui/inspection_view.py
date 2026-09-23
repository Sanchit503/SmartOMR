"""Markup for the source-page inspection workspace."""
from __future__ import annotations

import html
import urllib.parse


def _icon(name: str) -> str:
    return f'<img class="tool-icon" src="/static/icons/{name}.svg" alt="" aria-hidden="true">'


def _tool(identifier: str, icon: str, label: str) -> str:
    return f'<button id="{identifier}" class="icon-tool" type="button" title="{label}" aria-label="{label}">{_icon(icon)}</button>'


def inspection_body(state: dict) -> str:
    run_id = html.escape(state["run_id"], quote=True)
    exam_id = html.escape(state["exam_id"])
    source_url = "/artifact?path=" + urllib.parse.quote(state["inputs"]["scan_path"])
    return f"""
    <link rel="stylesheet" href="/static/inspection.css">
    <script src="/static/inspection.js" defer></script>
    <section id="inspection" data-run-id="{run_id}">
      <div class="inspection-heading">
        <div class="run-title"><div class="breadcrumb"><a href="/">Exams</a><span>/</span>Scan inspection</div><h1>{exam_id}</h1></div>
        <div class="run-actions">
          <a class="quiet-command" href="{source_url}">{_icon('download')}Source file</a>
          <form id="inspect-form" method="post" action="/runs/{run_id}/inspect" hidden><button class="secondary" type="submit">Resume Inspection</button></form>
          <form id="evaluate-form" method="post" action="/runs/{run_id}/evaluate" hidden><button type="submit">Run OCR &amp; Grouping{_icon('arrow-right')}</button></form>
          <a id="review-link" class="button" href="/runs/{run_id}" hidden>Student Review{_icon('arrow-right')}</a>
        </div>
      </div>
      <div class="inspection-summary">
        <div class="count"><strong id="total-count">-</strong><span>source pages</span></div>
        <div class="count"><i class="status-dot aligned"></i><strong id="aligned-count">-</strong><span>aligned</span></div>
        <div class="count"><i class="status-dot needs_review"></i><strong id="review-count">-</strong><span>need inspection</span></div>
        <div class="count"><i class="status-dot failed"></i><strong id="failed-count">-</strong><span>failed</span></div>
        <div class="processing-progress"><span id="progress-label" role="status">Loading inventory</span><progress id="progress" value="0" max="1" aria-label="Pages inspected"></progress></div>
      </div>
      <div id="connection-error" class="inspection-alert" role="alert" hidden></div>
      <div id="run-error" class="inspection-alert" role="alert" hidden></div>
      <div class="inspection-workspace">
        <aside class="source-sidebar" aria-label="Source pages">
          <div class="source-filters">
            <h2>Source pages <span id="filtered-count"></span></h2>
            <label class="visually-hidden" for="page-search">Search source page</label>
            <div class="search-field">{_icon('search')}<input id="page-search" type="search" placeholder="Page number" autocomplete="off"></div>
            <label class="visually-hidden" for="page-filter">Page status</label>
            <select id="page-filter"><option value="all">All pages</option><option value="aligned">Aligned</option><option value="needs_review">Needs inspection</option><option value="failed">Failed</option><option value="waiting">Waiting / processing</option></select>
          </div>
          <div id="page-list" class="source-list" aria-label="Page list"></div>
          <div id="no-pages" class="empty" hidden>No matching pages</div>
        </aside>
        <section class="document-panel" aria-label="Document viewer">
          <div class="document-toolbar">
            <div class="page-navigation">
              {_tool('previous-page', 'chevron-left', 'Previous source page')}
              <label class="visually-hidden" for="page-jump">Source page</label>
              <input id="page-jump" type="number" min="1" value="1" aria-label="Source page">
              <span id="page-total">/ -</span>
              {_tool('next-page', 'chevron-right', 'Next source page')}
            </div>
            <div class="view-switch" role="group" aria-label="Page image">
              <button type="button" data-view="original" aria-pressed="true">Original</button>
              <button type="button" data-view="aligned" aria-pressed="false">Aligned</button>
              <button type="button" data-view="overlay" aria-pressed="false">Overlay</button>
            </div>
            <div class="zoom-tools">
              {_tool('zoom-out', 'zoom-out', 'Zoom out')}
              <output id="zoom-label">100%</output>
              {_tool('zoom-in', 'zoom-in', 'Zoom in')}
              {_tool('zoom-fit', 'maximize', 'Fit page')}
            </div>
          </div>
          <div id="viewer" class="document-canvas" tabindex="0" aria-label="Scanned page">
            <div id="image-stage"><img id="page-image" alt="" hidden></div>
            <div id="viewer-empty" class="viewer-empty" role="status">Loading source page</div>
          </div>
          <div class="document-footer"><span id="image-label">Original</span><span id="source-label"></span></div>
        </section>
        <aside class="evidence-panel" aria-label="Page evidence">
          <div class="evidence-heading"><h2 id="selected-heading">Page evidence</h2><span id="page-status" class="state-label">Pending</span></div>
          <dl class="evidence-list">
            <div><dt>Source page</dt><dd id="source-number">-</dd></div>
            <div><dt>Detected sheet page</dt><dd id="template-number">Not detected</dd></div>
            <div><dt>Alignment score</dt><dd id="alignment-score">-</dd></div>
            <div><dt>Page-code score</dt><dd id="page-code-score">-</dd></div>
            <div><dt>Quality score</dt><dd id="quality-score">-</dd></div>
            <div><dt>Identity</dt><dd id="identity-state">Not evaluated</dd></div>
          </dl>
          <h3>Alignment checks</h3>
          <dl class="evidence-list checks">
            <div><dt>Geometry</dt><dd id="geometry-status">Pending</dd></div>
            <div><dt>Local registration</dt><dd id="local-status">Pending</dd></div>
            <div><dt>Image quality</dt><dd id="image-status">Pending</dd></div>
          </dl>
          <h3>Observations</h3>
          <ul id="observations" class="observations"><li>Inspection pending</li></ul>
          <a id="quality-report" class="quiet-command" target="_blank" rel="noopener" hidden>{_icon('file-text')}Quality report</a>
          <div class="source-metadata"><h3>Source record</h3><dl class="evidence-list"><div><dt>File</dt><dd id="file-name">-</dd></div><div><dt>Resolution</dt><dd id="render-dpi">-</dd></div></dl><details><summary>Input fingerprints</summary><label>Scan SHA-256</label><code id="source-hash"></code><label>Manifest SHA-256</label><code id="manifest-hash"></code></details></div>
        </aside>
      </div>
    </section>"""
