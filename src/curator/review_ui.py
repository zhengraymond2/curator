from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, Sequence

from .place_identification import PlaceIdentification, PreparedImage


@dataclass(frozen=True)
class LocationSuggestion:
    country_or_region: str
    place_name: str

    @property
    def label(self) -> str:
        return f"{self.country_or_region}/{self.place_name}"


@dataclass(frozen=True)
class ReviewItem:
    identification: PlaceIdentification
    prepared_images: tuple[PreparedImage, ...]
    file_count: int
    suggestions: tuple[LocationSuggestion, ...] = ()
    context_summary: str = ""


class ReviewState:
    def __init__(self, items: Sequence[ReviewItem]) -> None:
        self.items = list(items)
        self.index = 0
        self.decisions: dict[str, PlaceIdentification] = {}
        self.done = threading.Event()
        self.lock = threading.Lock()

    def payload(self) -> Mapping[str, object]:
        with self.lock:
            if self.index >= len(self.items):
                return {"done": True, "index": self.index, "total": len(self.items)}
            return review_item_payload(self.items[self.index], self.index, len(self.items))

    def decide(self, country_or_region: str | None, place_name: str | None) -> Mapping[str, object]:
        with self.lock:
            if self.index >= len(self.items):
                self.done.set()
                return {"done": True}

            item = self.items[self.index]
            original = item.identification
            country, place = resolve_location(
                country_or_region,
                place_name,
                original=original,
                suggestions=item.suggestions,
            )
            reviewed = replace(
                original,
                country_or_region=country,
                place_name=place,
                confidence=1.0 if (country, place) != (original.country_or_region, original.place_name) else original.confidence,
                is_unknown=False if place and not place.casefold().startswith("unknown") else original.is_unknown,
                rationale=f"User reviewed location: {country}/{place}",
            )
            self.decisions[original.group_id] = reviewed
            self.index += 1
            if self.index >= len(self.items):
                self.done.set()
                return {"done": True}
            return review_item_payload(self.items[self.index], self.index, len(self.items))


def review_place_identifications_in_browser(
    items: Sequence[ReviewItem],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> dict[str, PlaceIdentification]:
    if not items:
        return {}

    state = ReviewState(items)
    handler = make_handler(state)
    server = ThreadingHTTPServer((host, port), handler)
    actual_host, actual_port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://{actual_host}:{actual_port}/"

    try:
        if open_browser:
            webbrowser.open(url)
        print(f"Review UI: {url}")
        state.done.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return state.decisions


class BrowserReviewSession:
    def __init__(
        self,
        *,
        total: int,
        host: str = "127.0.0.1",
        port: int = 0,
        open_browser: bool = True,
    ) -> None:
        self.state = SequentialReviewState(total)
        self.handler = make_handler(self.state)
        self.server = ThreadingHTTPServer((host, port), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        actual_host, actual_port = self.server.server_address
        self.url = f"http://{actual_host}:{actual_port}/"
        self.open_browser = open_browser

    def __enter__(self) -> "BrowserReviewSession":
        self.thread.start()
        if self.open_browser:
            webbrowser.open(self.url)
        print(f"Review UI: {self.url}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.state.finish()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def review(self, item: ReviewItem, *, index: int) -> PlaceIdentification:
        return self.state.review(item, index=index)


class SequentialReviewState:
    def __init__(self, total: int) -> None:
        self.total = total
        self.index = 0
        self.current: ReviewItem | None = None
        self.decision: PlaceIdentification | None = None
        self.item_ready = threading.Event()
        self.item_done = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()

    def review(self, item: ReviewItem, *, index: int) -> PlaceIdentification:
        with self.lock:
            self.index = index
            self.current = item
            self.decision = None
            self.item_done.clear()
            self.item_ready.set()
        self.item_done.wait()
        with self.lock:
            if self.decision is None:
                raise RuntimeError("review UI closed before a decision was recorded")
            return self.decision

    def payload(self) -> Mapping[str, object]:
        with self.lock:
            if self.done.is_set():
                return {"done": True, "index": self.index, "total": self.total}
            if self.current is None:
                return {"loading": True, "done": False, "index": self.index, "total": self.total}
            return review_item_payload(self.current, self.index, self.total)

    def decide(self, country_or_region: str | None, place_name: str | None) -> Mapping[str, object]:
        with self.lock:
            if self.current is None:
                return {"loading": True, "done": False, "index": self.index, "total": self.total}
            item = self.current
            original = item.identification
            country, place = resolve_location(
                country_or_region,
                place_name,
                original=original,
                suggestions=item.suggestions,
            )
            self.decision = replace(
                original,
                country_or_region=country,
                place_name=place,
                confidence=1.0 if (country, place) != (original.country_or_region, original.place_name) else original.confidence,
                is_unknown=False if place and not place.casefold().startswith("unknown") else original.is_unknown,
                rationale=f"User reviewed location: {country}/{place}",
            )
            self.current = None
            self.item_ready.clear()
            self.item_done.set()
            return {"loading": True, "done": False, "index": self.index + 1, "total": self.total}

    def finish(self) -> None:
        with self.lock:
            self.current = None
            self.done.set()
            self.item_ready.set()
            self.item_done.set()


def review_item_payload(item: ReviewItem, index: int, total: int) -> Mapping[str, object]:
    identification = item.identification
    return {
        "done": False,
        "index": index,
        "total": total,
        "group_id": identification.group_id,
        "country_or_region": identification.country_or_region,
        "place_name": identification.place_name,
        "confidence": identification.confidence,
        "is_unknown": identification.is_unknown,
        "rationale": identification.rationale,
        "visual_evidence": list(identification.visual_evidence),
        "alternate_guesses": list(identification.alternate_guesses),
        "file_count": item.file_count,
        "context_summary": item.context_summary,
        "suggestions": [
            {
                "country_or_region": suggestion.country_or_region,
                "place_name": suggestion.place_name,
                "label": suggestion.label,
            }
            for suggestion in item.suggestions
        ],
        "images": [
            {
                "src": image.data_url,
                "filename": image.source_path.name,
                "path": str(image.source_path),
                "prepared_size": image.prepared_size,
                "encoded_bytes": image.encoded_bytes,
            }
            for image in item.prepared_images
        ],
    }


def resolve_location(
    country_or_region: str | None,
    place_name: str | None,
    *,
    original: PlaceIdentification,
    suggestions: Sequence[LocationSuggestion],
) -> tuple[str, str]:
    country = (country_or_region or "").strip() or original.country_or_region
    place = (place_name or "").strip() or original.place_name

    normalized_country = normalize_text(country)
    normalized_place = normalize_text(place)
    for suggestion in suggestions:
        if normalize_text(suggestion.country_or_region) == normalized_country and normalize_text(suggestion.place_name) == normalized_place:
            return suggestion.country_or_region, suggestion.place_name
    return country, place


def normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def make_handler(state: ReviewState) -> type[BaseHTTPRequestHandler]:
    class ReviewHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/" or self.path.startswith("/?"):
                self.send_text(HTML, content_type="text/html")
                return
            if self.path == "/api/state":
                self.send_json(state.payload())
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/decision":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw_body or "{}")
            except json.JSONDecodeError:
                self.send_error(400, "invalid JSON")
                return
            self.send_json(
                state.decide(
                    str(payload.get("country_or_region") or ""),
                    str(payload.get("place_name") or ""),
                )
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def send_json(self, payload: Mapping[str, object]) -> None:
            self.send_text(json.dumps(payload).encode("utf-8"), content_type="application/json")

        def send_text(self, body: str | bytes, *, content_type: str) -> None:
            encoded = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return ReviewHandler


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Curator Review</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f5f2;
      --ink: #1f2933;
      --muted: #667085;
      --line: #d7d4cc;
      --accent: #0f766e;
      --accent-ink: #ffffff;
      --panel: #ffffff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 3;
      display: grid;
      gap: 10px;
      padding: 14px 18px;
      background: rgba(246, 245, 242, 0.96);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(10px);
    }
    .topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
    }
    #progress {
      color: var(--muted);
      font-size: 14px;
      white-space: nowrap;
    }
    form {
      display: grid;
      grid-template-columns: minmax(140px, 220px) minmax(220px, 1fr) auto;
      gap: 8px;
      align-items: center;
    }
    input {
      width: 100%;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      font: inherit;
      background: #fff;
      color: var(--ink);
    }
    button {
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
    }
    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: var(--accent-ink);
    }
    .place-wrap { position: relative; }
    .suggestions {
      position: absolute;
      top: 42px;
      left: 0;
      right: 0;
      z-index: 5;
      max-height: 220px;
      overflow: auto;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 6px;
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.14);
    }
    .suggestion {
      display: grid;
      gap: 2px;
      padding: 8px 10px;
      cursor: pointer;
      border-bottom: 1px solid #eeeae2;
    }
    .suggestion:last-child { border-bottom: 0; }
    .suggestion.active {
      background: #e8f3f1;
    }
    .suggestion strong { font-size: 14px; }
    .suggestion span { color: var(--muted); font-size: 12px; }
    main {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 18px;
      padding: 18px;
    }
    .gallery {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
      gap: 8px;
      align-content: start;
    }
    figure {
      margin: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    figure img {
      display: block;
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: contain;
      background: #151515;
    }
    figcaption {
      padding: 6px 8px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    aside {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      align-self: start;
    }
    dl { margin: 0; display: grid; gap: 10px; }
    dt { font-size: 12px; color: var(--muted); }
    dd { margin: 2px 0 0; overflow-wrap: anywhere; }
    ul { margin: 4px 0 0 18px; padding: 0; color: var(--muted); }
    .done {
      max-width: 640px;
      margin: 80px auto;
      padding: 24px;
      text-align: center;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .loading {
      max-width: 640px;
      margin: 80px auto;
      padding: 24px;
      text-align: center;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
    }
    @media (max-width: 820px) {
      form { grid-template-columns: 1fr; }
      main { grid-template-columns: 1fr; }
      #progress { white-space: normal; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topline">
      <h1>Curator Review</h1>
      <div id="progress"></div>
    </div>
    <form id="review-form">
      <input id="country" autocomplete="off" placeholder="Country or region">
      <div class="place-wrap">
        <input id="place" autocomplete="off" placeholder="Place or album name">
        <div class="suggestions" id="suggestions" hidden></div>
      </div>
      <button class="primary" type="submit">Save / Continue</button>
    </form>
  </header>
  <main id="content">
    <section class="gallery" id="gallery"></section>
    <aside>
      <dl>
        <div><dt>Group</dt><dd id="group"></dd></div>
        <div><dt>Files</dt><dd id="file-count"></dd></div>
        <div><dt>Confidence</dt><dd id="confidence"></dd></div>
        <div><dt>Rationale</dt><dd id="rationale"></dd></div>
        <div><dt>Context</dt><dd id="context"></dd></div>
        <div><dt>Evidence</dt><dd><ul id="evidence"></ul></dd></div>
        <div><dt>Alternates</dt><dd><ul id="alternates"></ul></dd></div>
      </dl>
    </aside>
  </main>
  <script>
    let current = null;
    let activeSuggestion = null;

    async function loadState() {
      const response = await fetch('/api/state');
      current = await response.json();
      render();
    }

    function renderList(id, items) {
      const root = document.getElementById(id);
      root.innerHTML = '';
      for (const item of items || []) {
        const li = document.createElement('li');
        li.textContent = item;
        root.appendChild(li);
      }
    }

    function render() {
      if (!current || current.done) {
        document.body.innerHTML = '<div class="done"><h1>All groups reviewed</h1><p>You can return to the terminal.</p></div>';
        return;
      }
      if (current.loading) {
        document.getElementById('progress').textContent = `Preparing group ${Math.min((current.index || 0) + 1, current.total)} / ${current.total}`;
        const gallery = document.getElementById('gallery');
        if (gallery) gallery.innerHTML = '<div class="loading">Preparing the next album suggestion...</div>';
        for (const id of ['group', 'file-count', 'confidence', 'rationale', 'context']) {
          const node = document.getElementById(id);
          if (node) node.textContent = '';
        }
        renderList('evidence', []);
        renderList('alternates', []);
        setTimeout(loadState, 900);
        return;
      }
      document.getElementById('progress').textContent = `Group ${current.index + 1} / ${current.total}`;
      document.getElementById('country').value = current.country_or_region || '';
      document.getElementById('place').value = current.place_name || '';
      document.getElementById('group').textContent = current.group_id;
      document.getElementById('file-count').textContent = current.file_count;
      document.getElementById('confidence').textContent = `${Math.round((current.confidence || 0) * 100)}%${current.is_unknown ? ' · unknown' : ''}`;
      document.getElementById('rationale').textContent = current.rationale || '';
      document.getElementById('context').textContent = current.context_summary || '';
      renderList('evidence', current.visual_evidence);
      renderList('alternates', current.alternate_guesses);

      const gallery = document.getElementById('gallery');
      gallery.innerHTML = '';
      for (const image of current.images || []) {
        const figure = document.createElement('figure');
        const img = document.createElement('img');
        img.src = image.src;
        img.alt = image.filename;
        const caption = document.createElement('figcaption');
        caption.textContent = image.filename;
        figure.appendChild(img);
        figure.appendChild(caption);
        gallery.appendChild(figure);
      }
      document.getElementById('place').focus();
      document.getElementById('place').select();
      renderSuggestions();
    }

    async function submitDecision(country, place) {
      if (activeSuggestion && normalize(place) !== normalize(current.place_name)) {
        country = activeSuggestion.country_or_region;
        place = activeSuggestion.place_name;
      }
      const response = await fetch('/api/decision', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({country_or_region: country, place_name: place})
      });
      current = await response.json();
      render();
    }

    function normalize(value) {
      return (value || '').trim().toLocaleLowerCase();
    }

    function fuzzyScore(query, suggestion) {
      const q = normalize(query);
      const label = normalize(`${suggestion.country_or_region}/${suggestion.place_name}`);
      const place = normalize(suggestion.place_name);
      const country = normalize(suggestion.country_or_region);
      const countryInput = normalize(document.getElementById('country').value);
      if (!q) return country === countryInput ? 40 : 10;
      let score = 0;
      if (country === countryInput) score += 60;
      if (place === q || label === q) score += 100;
      else if (place.startsWith(q)) score += 80;
      else if (label.startsWith(q)) score += 70;
      else if (place.includes(q)) score += 50;
      else if (label.includes(q)) score += 40;
      else {
        let cursor = 0;
        for (const char of q) {
          cursor = label.indexOf(char, cursor);
          if (cursor === -1) return -1;
          cursor += 1;
        }
        score += 20;
      }
      return score;
    }

    function renderSuggestions() {
      const root = document.getElementById('suggestions');
      if (!current || !current.suggestions || !current.suggestions.length) {
        root.hidden = true;
        activeSuggestion = null;
        return;
      }
      const query = document.getElementById('place').value;
      const ranked = current.suggestions
        .map((suggestion) => ({suggestion, score: fuzzyScore(query, suggestion)}))
        .filter((item) => item.score >= 0)
        .sort((a, b) => b.score - a.score || a.suggestion.place_name.localeCompare(b.suggestion.place_name))
        .slice(0, 8);
      if (!ranked.length) {
        root.hidden = true;
        activeSuggestion = null;
        return;
      }
      activeSuggestion = ranked[0].suggestion;
      root.innerHTML = '';
      ranked.forEach((item, index) => {
        const div = document.createElement('div');
        div.className = `suggestion${index === 0 ? ' active' : ''}`;
        div.innerHTML = `<strong>${item.suggestion.place_name}</strong><span>${item.suggestion.country_or_region}</span>`;
        div.addEventListener('mousedown', (event) => {
          event.preventDefault();
          activeSuggestion = item.suggestion;
          document.getElementById('country').value = item.suggestion.country_or_region;
          document.getElementById('place').value = item.suggestion.place_name;
          root.hidden = true;
        });
        root.appendChild(div);
      });
      root.hidden = false;
    }

    document.getElementById('review-form').addEventListener('submit', (event) => {
      event.preventDefault();
      submitDecision(
        document.getElementById('country').value,
        document.getElementById('place').value
      );
    });

    document.getElementById('place').addEventListener('input', renderSuggestions);
    document.getElementById('country').addEventListener('input', renderSuggestions);

    window.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        document.getElementById('suggestions').hidden = true;
        activeSuggestion = null;
      }
      if (event.key === 'Enter') {
        document.getElementById('review-form').requestSubmit();
      }
    });

    loadState();
  </script>
</body>
</html>
"""
