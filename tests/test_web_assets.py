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


def test_records_carousel_has_prev_next_controls():
    js = _read("app.js")
    html = _read("index.html")
    # records: slow auto-advance plus manual prev/next stepping
    assert "stepRecord" in js
    assert "recordHover" in js                      # auto-rotate pauses on interaction
    assert "rec-cycle" in html                      # single cycle control
    assert ".rec-cycle" in _read("style.css")


def test_selecting_off_screen_plane_reveals_it_on_map():
    js = _read("app.js")
    # a selected plane is eased into the visible map area when it's off-screen
    assert "flyToFit" in js
    assert "isOnScreen" in js                       # do-nothing guard when visible
    assert "pendingFit" in js                       # fresh autoselect reveals too


def test_record_replay_controls_present():
    html = _read("index.html")
    css = _read("style.css")
    assert 'id="rec-replay"' in html          # hourglass on the records ticker
    assert 'id="replay-banner"' in html        # "replaying session … / return to live"
    assert "#replay-banner" in css


def test_left_pane_structure():
    js = _read("app.js")
    html = _read("index.html")
    css = _read("style.css")
    # compact list: a column-header row and a tight stats strip render function
    assert "listHead" in js                         # sticky column headers
    assert "renderStats" in js                      # detail stats strip
    assert 'id="stats"' in html                     # plot-first detail's stats element
    # detail is a dismissable bottom sheet on mobile
    assert 'id="sheet-close"' in html
    assert "#rail.selected #detail" in css          # sheet slides up on selection
    # mobile: swipe-to-close drawer + tap-the-thumbnail photo lightbox
    assert "openLightbox" in js and 'id="lightbox"' in html
    assert "touchstart" in js and "touchend" in js   # swipe-to-close
