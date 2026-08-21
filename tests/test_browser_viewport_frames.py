"""browser tool: viewport control, frame-aware actions, honest screenshots.

The failure these pin down is not "a feature was missing". It is that the tool
reported success while describing a page the agent could not see: get_text
deleted the consent iframe from the live DOM and returned the document
underneath, so the overlay read as absent AND became unclickable.
"""

import struct

import pytest

import src.tools.browser as br
from src.core.base import ToolContext


def _png(width: int, height: int) -> bytes:
    """A byte string with a valid PNG signature and IHDR dimensions."""
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00" + b"rest")


def _ctx():
    return ToolContext(agent_id="atlas")


class FakeLocator:
    def __init__(self, page, selector, frame="", texts=None, fail=False):
        self._page, self._selector, self._frame = page, selector, frame
        self._texts = texts if texts is not None else []
        self._fail = fail

    @property
    def first(self):
        return self

    def nth(self, i):
        return FakeLocator(self._page, self._selector, self._frame,
                           texts=[self._texts[i]])

    async def count(self):
        return len(self._texts)

    async def inner_text(self):
        return self._texts[0] if self._texts else ""

    async def click(self, timeout=None):
        if self._fail:
            raise RuntimeError("not found")
        self._page.clicks.append((self._frame, self._selector))

    async def fill(self, text, timeout=None):
        if self._fail:
            raise RuntimeError("not found")
        self._page.fills.append((self._frame, self._selector, text))

    async def select_option(self, value, timeout=None):
        self._page.selects.append((self._frame, self._selector, value))


class FakeFrameLocator:
    def __init__(self, page, frame):
        self._page, self._frame = page, frame

    def locator(self, selector):
        texts = self._page.frame_texts.get(self._frame, [])
        hit = selector in self._page.frame_hits.get(self._frame, ())
        if selector == "body":
            return FakeLocator(self._page, selector, self._frame,
                               texts=texts, fail=not texts)
        return FakeLocator(self._page, selector, self._frame,
                           texts=texts, fail=not hit)


class FakeElement:
    def __init__(self, box, attrs):
        self._box, self._attrs = box, attrs

    async def bounding_box(self):
        return self._box

    async def evaluate(self, js):
        return self._attrs


class FakeChildFrame:
    """A Playwright Frame: has locator()/get_by_role(), is not the main frame."""

    def __init__(self, page, name, accepts=()):
        self._page, self.name, self.url = page, name, f"https://cmp/{name}"
        self._accepts = accepts

    def locator(self, selector):
        return FakeLocator(self._page, selector, self.name,
                           fail=selector not in self._accepts)

    def get_by_role(self, role, name=None, exact=False):
        return FakeLocator(self._page, f"role={name}", self.name, fail=True)


class FakePage:
    def __init__(self, *, text="hello world", iframes=(), frames=(),
                 page_height=800, client_width=1280, title="Test Page",
                 main_hits=()):
        self.url = "https://example.test/"
        self._title = title
        self._text = text
        self._iframes = list(iframes)
        self._page_height = page_height
        self._client_width = client_width
        self._main_hits = tuple(main_hits)
        self.evaluated: list[str] = []
        self.clicks: list[tuple] = []
        self.fills: list[tuple] = []
        self.selects: list[tuple] = []
        self.screenshots: list[dict] = []
        self.viewport_sets: list[dict] = []
        self.frame_texts: dict[str, list[str]] = {}
        self.frame_hits: dict[str, tuple] = {}
        self.main_frame = object()
        self.frames = [self.main_frame, *frames]

    async def title(self):
        return self._title

    async def goto(self, url, **kw):
        self.url = url

    async def wait_for_timeout(self, ms):
        return None

    async def set_viewport_size(self, size):
        self.viewport_sets.append(size)

    async def evaluate(self, js, *a):
        self.evaluated.append(js)
        if "scrollHeight" in js:
            return self._page_height
        if "clientWidth" in js:
            return self._client_width
        if "innerText" in js:
            return self._text
        return None

    async def query_selector_all(self, selector):
        return list(self._iframes) if selector == "iframe" else []

    async def screenshot(self, full_page=False, clip=None):
        self.screenshots.append({"full_page": full_page, "clip": clip})
        if clip:
            return _png(int(clip["width"]), int(clip["height"]))
        return _png(1280, self._page_height if full_page else 720)

    def locator(self, selector):
        return FakeLocator(self, selector, "", texts=[self._text],
                           fail=selector not in self._main_hits)

    def frame_locator(self, selector):
        return FakeFrameLocator(self, selector)

    def get_by_role(self, role, name=None, exact=False):
        return FakeLocator(self, f"role={name}", "", fail=True)


def _overlay_iframe(width=600, height=400, ident="sp_message_iframe_123"):
    return FakeElement({"x": 0, "y": 0, "width": width, "height": height},
                       {"id": ident, "title": "", "z": "2147483647",
                        "pos": "fixed"})


@pytest.fixture
def session(monkeypatch, tmp_path):
    """Install a fake session and route KBOTS_TMP at a temp dir."""
    monkeypatch.setattr(br, "KBOTS_TMP", tmp_path)
    state: dict = {}

    async def _get(session_id, spec=None):
        return state["sess"]

    monkeypatch.setattr(br, "_get_or_create_session", _get)
    monkeypatch.setattr(br, "_validate_url", lambda url: "")

    def _install(page, viewport=None):
        state["sess"] = {"page": page, "viewport": dict(viewport or br.DEFAULT_VIEWPORT),
                         "context": None, "browser": None}
        return state["sess"]

    return _install


# --- viewport resolution ---------------------------------------------------

def test_explicit_dimensions_and_scale_are_used():
    spec = br.resolve_viewport(width=1920, height=1080, scale=2.0)
    assert (spec["width"], spec["height"], spec["scale"]) == (1920, 1080, 2.0)


def test_a_device_preset_carries_its_own_ua_and_touch_flags():
    spec = br.resolve_viewport(device="iphone")
    assert spec["mobile"] is True
    assert "iPhone" in spec["ua"]
    assert spec["scale"] > 1


def test_explicit_values_override_the_preset():
    """So a preset can be nudged without defining a new one."""
    spec = br.resolve_viewport(width=430, device="iphone")
    assert spec["width"] == 430 and spec["mobile"] is True


def test_an_unknown_device_names_the_ones_that_exist():
    err = br.resolve_viewport(device="pixelfold")
    assert isinstance(err, str) and "desktop-1920" in err and "iphone" in err


@pytest.mark.parametrize("kw", [{"width": 50}, {"height": 9000}, {"scale": 12.0}])
def test_out_of_range_viewports_are_refused(kw):
    assert isinstance(br.resolve_viewport(**kw), str)


def test_the_default_is_unchanged_when_nothing_is_asked_for():
    spec = br.resolve_viewport()
    assert (spec["width"], spec["height"], spec["scale"]) == (1280, 720, 1.0)


# --- screenshots say what they produced ------------------------------------

def test_png_size_reads_the_ihdr_header():
    assert br._png_size(_png(1920, 4321)) == (1920, 4321)
    assert br._png_size(b"not a png") == (0, 0)


async def test_a_screenshot_reports_its_pixel_dimensions(session):
    page = FakePage()
    session(page)
    out = await br.browser(_ctx(), "screenshot")
    assert "1280x720px" in out
    assert ".png" in out


async def test_a_long_full_page_is_tiled_rather_than_returned_as_a_strip(session):
    """A 1280x20000 image is scaled to an unreadable sliver by any chat client,
    and the old result gave no way to know that before sending it."""
    page = FakePage(page_height=20000)
    session(page)
    out = await br.browser(_ctx(), "screenshot", full_page=True)

    assert "20000px" in out and "tiles" in out
    assert len(page.screenshots) > 1
    assert all(s["clip"] is not None for s in page.screenshots)


async def test_tiling_says_when_it_did_not_reach_the_bottom(session):
    page = FakePage(page_height=10_000_000)
    session(page)
    out = await br.browser(_ctx(), "screenshot", full_page=True)

    assert "NOT captured" in out
    assert len(page.screenshots) == br.MAX_TILES


async def test_a_short_full_page_is_a_single_image(session):
    page = FakePage(page_height=1500)
    session(page)
    out = await br.browser(_ctx(), "screenshot", full_page=True)

    assert "tiles" not in out
    assert len(page.screenshots) == 1 and page.screenshots[0]["clip"] is None


async def test_max_height_forces_tiling_earlier(session):
    page = FakePage(page_height=3000)
    session(page)
    await br.browser(_ctx(), "screenshot", full_page=True, max_height=1000)
    assert len(page.screenshots) == 3


# --- get_text must not lie, and must not destroy the page ------------------

async def test_get_text_does_not_mutate_the_live_dom(session):
    """The old implementation removed script/style/iframe/svg from the REAL
    document. It deleted the consent iframe it should have reported, so the
    dialog could not be clicked afterwards either."""
    page = FakePage(text="body text")
    session(page)
    await br.browser(_ctx(), "get_text")

    assert page.evaluated, "get_text ran no page script at all"
    assert not any("remove()" in js for js in page.evaluated)


async def test_get_text_warns_that_a_modal_iframe_is_covering_the_page(session):
    page = FakePage(text="article text", iframes=[_overlay_iframe()])
    session(page)
    out = await br.browser(_ctx(), "get_text")

    assert "MAIN DOCUMENT ONLY" in out
    assert "sp_message_iframe_123" in out
    assert "dismiss_consent" in out
    # And the warning comes first: an agent that reads the text and stops must
    # not have already been told the page is clear.
    assert out.index("MAIN DOCUMENT") < out.index("article text")


async def test_get_text_is_quiet_when_no_overlay_is_present(session):
    page = FakePage(text="article text")
    session(page)
    out = await br.browser(_ctx(), "get_text")
    assert "MAIN DOCUMENT ONLY" not in out and "article text" in out


async def test_a_small_iframe_is_not_reported_as_an_overlay(session):
    """Ad slots and trackers are iframes too. Warning on every one of them
    trains the agent to ignore the warning."""
    page = FakePage(iframes=[_overlay_iframe(width=120, height=60, ident="ad")])
    session(page)
    assert "MAIN DOCUMENT ONLY" not in await br.browser(_ctx(), "get_text")


async def test_get_text_can_read_inside_a_frame(session):
    page = FakePage()
    page.frame_texts["iframe[id*=sp_message]"] = ["We use cookies. Godkänn alla?"]
    session(page)
    out = await br.browser(_ctx(), "get_text", frame="iframe[id*=sp_message]")
    assert "Godkänn alla" in out


# --- acting inside a frame -------------------------------------------------

async def test_click_enters_the_frame_when_one_is_given(session):
    page = FakePage()
    page.frame_hits["iframe[id*=sp_message]"] = ('button[title*="Godkänn"]',)
    session(page)
    out = await br.browser(_ctx(), "click", selector='button[title*="Godkänn"]',
                           frame="iframe[id*=sp_message]")

    assert "Clicked" in out
    assert page.clicks == [("iframe[id*=sp_message]", 'button[title*="Godkänn"]')]


async def test_fill_enters_the_frame_when_one_is_given(session):
    page = FakePage()
    page.frame_hits["#login"] = ("input[name=email]",)
    session(page)
    await br.browser(_ctx(), "fill", selector="input[name=email]",
                     text="a@b.c", frame="#login")
    assert page.fills == [("#login", "input[name=email]", "a@b.c")]


async def test_a_failed_text_click_points_at_the_iframe_it_could_not_enter(session):
    """This is the message that would have saved three round-trips: text search
    does not cross into frames, and the page had a modal in one."""
    page = FakePage(iframes=[_overlay_iframe()])
    session(page)
    out = await br.browser(_ctx(), "click", text="Godkänn alla")

    assert "Could not find" in out
    assert "frame=" in out and "dismiss_consent" in out


# --- dismiss_consent -------------------------------------------------------

async def test_consent_is_accepted_inside_a_child_frame(session):
    page = FakePage()
    frame = FakeChildFrame(page, "sp_message_iframe_1",
                           accepts=('button[title*="Godkänn" i]',))
    page.frames = [page.main_frame, frame]
    session(page)

    out = await br.browser(_ctx(), "dismiss_consent")
    assert "Consent accepted" in out
    assert page.clicks and page.clicks[0][0] == "sp_message_iframe_1"


async def test_dismiss_consent_reports_honestly_when_nothing_matched(session):
    page = FakePage(iframes=[_overlay_iframe()])
    session(page)
    out = await br.browser(_ctx(), "dismiss_consent")

    assert "No consent dialog matched" in out
    assert "sp_message_iframe_123" in out, "must say what is still on the page"


# --- resize ----------------------------------------------------------------

async def test_resize_at_the_same_scale_keeps_the_page_and_its_state(session):
    page = FakePage()
    session(page)
    out = await br.browser(_ctx(), "resize", width=1920, height=1080)

    assert page.viewport_sets == [{"width": 1920, "height": 1080}]
    assert "1920x1080" in out and "resized" in out


async def test_changing_the_scale_rebuilds_the_context_and_says_so(session, monkeypatch):
    page = FakePage()
    sess = session(page)
    rebuilt = {}

    async def _new_context(browser, spec, storage_state=None):
        rebuilt["spec"] = spec
        return object(), FakePage()

    monkeypatch.setattr(br, "_new_context", _new_context)
    out = await br.browser(_ctx(), "resize", scale=2.0)

    assert rebuilt["spec"]["scale"] == 2.0
    assert "cookies carried over" in out
    assert sess["viewport"]["scale"] == 2.0
    assert page.viewport_sets == [], "a scale change is not a live resize"


async def test_resize_to_the_current_size_does_nothing(session):
    page = FakePage()
    session(page)
    out = await br.browser(_ctx(), "resize", width=1280, height=720)
    assert "already" in out and page.viewport_sets == []


async def test_an_unknown_device_is_refused_before_the_session_is_touched(session):
    page = FakePage()
    session(page)
    out = await br.browser(_ctx(), "open", url="https://example.test/",
                           device="nokia-3310")
    assert "Unknown device" in out
    assert page.url == "https://example.test/", "must not have navigated"


# --- open ------------------------------------------------------------------

async def test_open_reports_the_viewport_it_actually_used(session):
    page = FakePage()
    session(page, viewport={**br.DEFAULT_VIEWPORT, "width": 1920,
                            "height": 1080, "scale": 2.0})
    out = await br.browser(_ctx(), "open", url="https://example.test/x")
    assert "1920x1080 @ 2.0x" in out


async def test_open_flags_a_consent_wall_immediately(session):
    page = FakePage(iframes=[_overlay_iframe()])
    session(page)
    out = await br.browser(_ctx(), "open", url="https://example.test/x")
    assert "dismiss_consent" in out


async def test_resize_is_a_known_action():
    """It used to answer 'Unknown action: resize'."""
    out = await br.browser(_ctx(), "definitely-not-an-action")
    assert "resize" in out and "dismiss_consent" in out
