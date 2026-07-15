import os

WEB = os.path.join(os.path.dirname(__file__), "..", "doppler1090", "web")


def _read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as fh:
        return fh.read()


def test_index_loads_leaflet_and_app():
    html = _read("index.html")
    assert "leaflet" in html.lower()          # map library present
    assert "app.js" in html
    assert 'id="map"' in html
    # Leaflet is vendored locally so the dashboard loads offline (no CDN)
    assert "vendor/leaflet/leaflet.js" in html
    assert "unpkg.com" not in html


def test_app_js_polls_state_and_has_color_logic():
    js = _read("app.js")
    assert "/api/state" in js                 # polls the data endpoint
    assert "ground_track" in js               # renders tracks
    assert "canvas" in js.lower() or "getContext" in js  # draws the plot


def test_css_exists():
    assert "#map" in _read("style.css")


def test_app_js_highlights_selected_aircraft():
    js = _read("app.js")
    assert "planeIcon" in js               # rotated airplane icon
    assert "divIcon" in js                 # built as a Leaflet divIcon
    assert "setIcon" in js                 # icon updated on selection/heading


def test_records_carousel_is_clickable():
    js = _read("app.js")
    # the records carousel advances on click, and a manual tap resets the timer
    assert "recordTimer" in js
    assert "h-record" in js and "addEventListener('click'" in js.replace('"', "'")
    # a tappable affordance in the styles
    assert "cursor: pointer" in _read("style.css") or "cursor:pointer" in _read("style.css")
